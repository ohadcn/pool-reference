from pathlib import Path
from typing import Optional, Set, List, Tuple, Dict

import aiosqlite
from blspy import G1Element
from chia.pools.pool_wallet_info import PoolState
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.util.ints import uint64
from chia.util.bech32m import encode_puzzle_hash

from .abstract import AbstractPoolStore
from ..record import FarmerRecord
from ..util import RequestMetadata


class SqlitePoolStore(AbstractPoolStore):
    """
    Pool store based on SQLite.
    """

    def __init__(self, db_path: Path = Path("pooldb.sqlite")):
        super().__init__()
        self.db_path = db_path
        self.connection: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.connection = await aiosqlite.connect(self.db_path)
        await self.connection.execute("pragma journal_mode=wal")
        await self.connection.execute("pragma synchronous=2")
        await self.connection.execute(
            (
                "CREATE TABLE IF NOT EXISTS farmer("
                "launcher_id text PRIMARY KEY,"
                " p2_singleton_puzzle_hash text,"
                " delay_time bigint,"
                " delay_puzzle_hash text,"
                " authentication_public_key text,"
                " singleton_tip blob,"
                " singleton_tip_state blob,"
                " points bigint,"
                " difficulty bigint,"
                " payout_instructions text,"
                " is_pool_member tinyint)"
            )
        )

        await self.connection.execute(
            "CREATE TABLE IF NOT EXISTS partial(launcher_id text, timestamp bigint, difficulty bigint)"
        )

        await self.connection.execute("CREATE INDEX IF NOT EXISTS scan_ph on farmer(p2_singleton_puzzle_hash)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS timestamp_index on partial(timestamp)")
        await self.connection.execute("CREATE INDEX IF NOT EXISTS launcher_id_index on partial(launcher_id)")

        await self.connection.commit()

    @staticmethod
    def _row_to_farmer_record(row) -> FarmerRecord:
        return FarmerRecord(
            bytes.fromhex(row[0]),
            bytes.fromhex(row[1]),
            row[2],
            bytes.fromhex(row[3]),
            G1Element.from_bytes(bytes.fromhex(row[4])),
            CoinSpend.from_bytes(row[5]),
            PoolState.from_bytes(row[6]),
            row[7],
            row[8],
            row[9],
            True if row[10] == 1 else False,
        )

    async def add_farmer_record(self, farmer_record: FarmerRecord, metadata: RequestMetadata):
        # Find the launcher_id exists.
        cursor = await self.connection.execute(
            "SELECT * from farmer where launcher_id=?",
            (farmer_record.launcher_id.hex(),),
        )
        row = await cursor.fetchone()
        # Insert for None
        if row is None:
            cursor = await self.connection.execute(
                f"INSERT INTO farmer VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    farmer_record.launcher_id.hex(),
                    farmer_record.p2_singleton_puzzle_hash.hex(),
                    farmer_record.delay_time,
                    farmer_record.delay_puzzle_hash.hex(),
                    bytes(farmer_record.authentication_public_key).hex(),
                    bytes(farmer_record.singleton_tip),
                    bytes(farmer_record.singleton_tip_state),
                    farmer_record.points,
                    farmer_record.difficulty,
                    farmer_record.payout_instructions,
                    int(farmer_record.is_pool_member),
                ),
            )
        # update for Exist
        else:
            cursor = await self.connection.execute(
                f"UPDATE farmer SET "
                f"p2_singleton_puzzle_hash=?, "
                f"delay_time=?, "
                f"delay_puzzle_hash=?, "
                f"authentication_public_key=?, "
                f"singleton_tip=?, "
                f"singleton_tip_state=?, "
                f"payout_instructions=?, "
                f"is_pool_member=? "
                f"WHERE launcher_id=?",
                (
                    farmer_record.p2_singleton_puzzle_hash.hex(),
                    farmer_record.delay_time,
                    farmer_record.delay_puzzle_hash.hex(),
                    bytes(farmer_record.authentication_public_key).hex(),
                    bytes(farmer_record.singleton_tip),
                    bytes(farmer_record.singleton_tip_state),
                    farmer_record.payout_instructions,
                    int(farmer_record.is_pool_member),
                    farmer_record.launcher_id.hex(),
                ),
            )
        await cursor.close()
        await self.connection.commit()

    async def get_farmer_record(self, launcher_id: bytes32) -> Optional[FarmerRecord]:
        # TODO(pool): use cache
        cursor = await self.connection.execute(
            "SELECT * from farmer where launcher_id=?",
            (launcher_id.hex(),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return self._row_to_farmer_record(row)

    async def get_farmer_record_for_all_farmers(self) -> Optional[list]:
        # TODO(pool): use cache
        cursor = await self.connection.execute(
            '''SELECT farmer.difficulty,points,launcher_id,
                SUM(case when strftime('%s', 'now','-1 day')<timestamp and source='p' then 1 else 0 end) AS partials,
                COALESCE(SUM(case when strftime('%s', 'now','-1 day')<timestamp and source='p' then p.difficulty else 0 end), 0) AS points24,
                payout_instructions,
                MIN(timestamp) as joinDate,
                p2_singleton_puzzle_hash,
				COUNT(DISTINCT harvester),
                SUM(case when strftime('%s', 'now','-1 day')<timestamp and source='i' then 1 else 0 end) AS invalid_partials
				FROM farmer
                JOIN (SELECT launcher_id,harvester,difficulty,timestamp,'p' as source FROM partial
                UNION SELECT launcher_id,harvester,difficulty,timestamp,'i' as source FROM invalid_partial) p USING(launcher_id)
                WHERE is_pool_member=1
                GROUP BY launcher_id''',
            (),
        )
        rows = await cursor.fetchall()
        if rows is None:
            return None
        return [{
            "difficulty": row[0],
            "points": row[1],
            "launcher_id": row[2],
            "partials24": row[3],
            "points24": row[4],
            "payout_address": encode_puzzle_hash(bytes.fromhex(row[5]), "xch"),
            "joinDate": row[6],
            "puzzle_hash": row[7],
            "harvesters": row[8],
            "invalid_partials": row[9]
        } for row in rows]

    async def update_difficulty(self, launcher_id: bytes32, difficulty: uint64):
        cursor = await self.connection.execute(
            f"UPDATE farmer SET difficulty=? WHERE launcher_id=?", (difficulty, launcher_id.hex())
        )
        await cursor.close()
        await self.connection.commit()

    async def update_singleton(
        self,
        launcher_id: bytes32,
        singleton_tip: CoinSpend,
        singleton_tip_state: PoolState,
        is_pool_member: bool,
    ):
        entry = (bytes(singleton_tip), bytes(singleton_tip_state), int(is_pool_member), launcher_id.hex())
        cursor = await self.connection.execute(
            f"UPDATE farmer SET singleton_tip=?, singleton_tip_state=?, is_pool_member=? WHERE launcher_id=?",
            entry,
        )
        await cursor.close()
        await self.connection.commit()

    async def get_pay_to_singleton_phs(self) -> Set[bytes32]:
        cursor = await self.connection.execute("SELECT p2_singleton_puzzle_hash from farmer")
        rows = await cursor.fetchall()
        await cursor.close()

        all_phs: Set[bytes32] = set()
        for row in rows:
            all_phs.add(bytes32(bytes.fromhex(row[0])))
        return all_phs

    async def get_farmer_records_for_p2_singleton_phs(self, puzzle_hashes: Set[bytes32]) -> List[FarmerRecord]:
        if len(puzzle_hashes) == 0:
            return []
        puzzle_hashes_db = tuple([ph.hex() for ph in list(puzzle_hashes)])
        cursor = await self.connection.execute(
            f'SELECT * from farmer WHERE p2_singleton_puzzle_hash in ({"?," * (len(puzzle_hashes_db) - 1)}?) ',
            puzzle_hashes_db,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_farmer_record(row) for row in rows]

    async def get_farmer_points_and_payout_instructions(self) -> List[Tuple[uint64, bytes, uint64]]:
        cursor = await self.connection.execute(f"""
            SELECT points, payout_instructions,MIN(timestamp) as joinDate,
            COALESCE(SUM(case when strftime('%s', 'now','-1 day')<timestamp then partial.difficulty else 0 end), 0) AS points24
            FROM farmer LEFT OUTER JOIN partial USING(launcher_id) WHERE farmer.is_pool_member=1 GROUP BY launcher_id having points>0
            """)
        rows = await cursor.fetchall()
        await cursor.close()

        accumulated: Dict[bytes32, uint64, uint64] = {}
        for row in rows:
            points: uint64 = uint64(row[3])
            joined: uint64 = uint64(row[2])
            ph: bytes32 = bytes32(bytes.fromhex(row[1]))
            if ph in accumulated:
                accumulated[ph][0] += points
                accumulated[ph][1] = min(joined, accumulated[ph][1])
            else:
                accumulated[ph] = [points, joined]

        ret: List[Tuple[uint64, bytes32, uint64]] = []
        for ph, total_points in accumulated.items():
            ret.append((total_points[0], ph, total_points[1]))
        return ret

    async def clear_farmer_points(self) -> None:
        cursor = await self.connection.execute(f"UPDATE farmer set points=0")
        await cursor.close()
        await self.connection.commit()

    async def add_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64, harvester: bytes32):
        cursor = await self.connection.execute(
            "INSERT into partial VALUES(?, ?, ?, ?)",
            (launcher_id.hex(), timestamp, difficulty, harvester.hex()),
        )
        await cursor.close()
        cursor = await self.connection.execute(
            f"UPDATE farmer SET points=points+? WHERE launcher_id=?", (difficulty, launcher_id.hex())
        )
        await cursor.close()
        await self.connection.commit()

    async def store_invalid_partial(self, launcher_id: bytes32, timestamp: uint64, difficulty: uint64, harvester: bytes32, reason: str):
        cursor = await self.connection.execute(
            "INSERT into invalid_partial VALUES(?, ?, ?, ?, ?)",
            (launcher_id.hex(), timestamp, difficulty, harvester.hex(), reason),
        )
        await cursor.close()
        await self.connection.commit()

    async def get_recent_partials(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        cursor = await self.connection.execute(
            "SELECT timestamp, difficulty from partial WHERE launcher_id=? ORDER BY timestamp DESC LIMIT ?",
            (launcher_id.hex(), count),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        ret: List[Tuple[uint64, uint64]] = [(uint64(timestamp), uint64(difficulty)) for timestamp, difficulty in rows]
        return ret

    async def get_points_by_date(self, launcher_id: bytes32, count: int) -> List[Tuple[uint64, uint64]]:
        cursor = await self.connection.execute(
            """
            SELECT strftime('%H:%M',datetime(timestamp, 'unixepoch'), 'localtime') as date,
            SUM(difficulty) as points
            FROM partial WHERE launcher_id=? AND strftime('%s', 'now','-1 day')<timestamp
            GROUP BY strftime('%Y.%m.%d.%H.%M',datetime(timestamp, 'unixepoch')) LIMIT ?
            """,
            (launcher_id.hex(), count * 60),
        )
        rows = await cursor.fetchall()
        ret: List[Tuple[str, uint64]] = [(str(date), uint64(points)) for date, points in rows]
        return ret

    async def get_recent_partials_all_farmers(self, count: int) -> List[Tuple[uint64, uint64, str]]:
        cursor = await self.connection.execute(
            "SELECT timestamp, difficulty, launcher_id from partial ORDER BY timestamp DESC LIMIT ?",
            (count,),
        )
        rows = await cursor.fetchall()
        ret: List[Tuple[uint64, uint64, str]] = [(uint64(timestamp), uint64(difficulty), launcher_id) for timestamp, difficulty, launcher_id in rows]
        return ret
