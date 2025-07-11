import binascii
import requests
from contextlib import contextmanager
from config import SingletonMeta, Config

import apsw

from nicefetcher import utils

import xxhash

def hash_string_and_number(input_bytes: bytes, input_num: int) -> bytes:
    # same XXH3-64 little-endian combinator you used in fetcher.py
    combined = input_bytes + input_num.to_bytes(8, "little")
    h = xxhash.xxh3_64_intdigest(combined)
    return h.to_bytes(8, "little")


def rowtracer(cursor, sql):
    """Converts fetched SQL data into dict-style"""
    return {
        name: (bool(value) if str(field_type) == "BOOL" else value)
        for (name, field_type), value in zip(cursor.getdescription(), sql)
    }


def apsw_connect(filename):
    """Connect to SQLite database with optimal settings"""
    db = apsw.Connection(filename)
    cursor = db.cursor()
    cursor.execute("PRAGMA page_size = 4096")
    cursor.execute("PRAGMA auto_vacuum = 0")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.execute("PRAGMA journal_size_limit = 6144000")
    cursor.execute("PRAGMA cache_size = 10000")
    cursor.execute("PRAGMA defer_foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA locking_mode = NORMAL")

    db.setbusytimeout(5000)
    db.setrowtrace(rowtracer)
    cursor.close()
    return db


def get_shard_id(utxo_id):
    """Determine the shard ID based on the first byte of utxo_id"""
    return utxo_id[0] % 10


class APSWConnectionPool():
    def __init__(self, db_file):
        self.connections = []
        self.db_file = db_file
        self.closed = False
        self.pool_size = 10

    @contextmanager
    def connection(self):
        if self.connections:
            # Reusing connection
            db = self.connections.pop(0)
        else:
            # New db connection
            db = apsw_connect(self.db_file)
        try:
            yield db
        finally:
            if self.closed:
                db.close()
            elif len(self.connections) < self.pool_size:
                # Add connection to pool
                self.connections.append(db)
            else:
                # Too much connections in the pool: closing connection
                db.close()

    def close(self):
        self.closed = True
        while len(self.connections) > 0:
            db = self.connections.pop()
            db.close()


class ShardedConnectionPool:
    """Connection pool for multiple sharded databases"""

    def __init__(self, base_path):
        self.base_path = base_path
        self.pools = {}
        self.closed = False

    def get_shard_pool(self, shard_id):
        """Get or create a connection pool for a specific shard"""
        if shard_id not in self.pools:
            db_file = f"{self.base_path}/rb1ts_balances_{shard_id}.db"
            print(f"Opening shard {shard_id} at {db_file}")
            self.pools[shard_id] = APSWConnectionPool(db_file)
        return self.pools[shard_id]

    @contextmanager
    def shard_connection(self, shard_id):
        """Get a connection for a specific shard"""
        pool = self.get_shard_pool(shard_id)
        with pool.connection() as conn:
            yield conn

    def close(self):
        """Close all connection pools"""
        self.closed = True
        for pool in self.pools.values():
            pool.close()


def utxo_to_utxo_id(txid_hex: str, n: int) -> bytes:
    """
    Convert RPC hex TXID + vout into the same little-endian xxh3 key
    your parser used when writing to the DB.
    """
    # 1) Turn into bytes and reverse to little-endian
    be = bytes.fromhex(txid_hex)
    tx_bytes = be[::-1]

    # 2) Apply the XXH3‐64+LE routine
    return hash_string_and_number(tx_bytes, n)


class Rb1tsQueries:
    """
    Classe pour effectuer des requêtes sur les données RB1TS
    Remarque: Cette implémentation accède directement aux bases de données
    mais est compatible avec l'architecture où ces bases sont gérées par des processus séparés
    """

    def __init__(self, db_file=None):
        base_path = Config()["BALANCES_STORE"]
        if db_file is None:
            db_file = f"{base_path}/rb1ts_indexes.db"
        self.base_path = base_path
        self.pool = APSWConnectionPool(db_file)
        self.shard_pools = ShardedConnectionPool(base_path)

        # Cache for statistics
        self._stats_cache = None
        self._stats_cache_block_height = 0
        self._latest_nicehashes_cache = None
        self._latest_nicehashes_cache_block_height = 0
    
    def get_balance_by_address(self, address):
        """
        Fetch UTXOs for an address using mempool.space API,
        convert them to utxo_id, and sum up their balances.

        Args:
            address (str): Bitcoin address

        Returns:
            dict: Balance information including total balance and UTXOs
        """
        # Get UTXOs from mempool.space API
        try:
            api_url = f"https://blockbook.b1tcore.org/api/v2/utxo/{address}?confirmed=false"
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            utxos = response.json()
        except requests.RequestException as e:
            print(f"Error fetching UTXOs for address {address}: {e}")
            return {"total_balance": 0, "utxos": []}

        print("utxos", utxos)

        total_balance = 0
        utxo_details = []

        for utxo in utxos:
            txid = utxo.get("txid")
            vout = utxo.get("vout")

            if txid and vout is not None:
                # Convert to utxo_id
                utxo_id = utxo_to_utxo_id(txid, vout)
                print("utxo_id", utxo_id)
                shard_id = get_shard_id(utxo_id)

                # Query the balance from the appropriate shard
                with self.shard_pools.shard_connection(shard_id) as db:
                    cursor = db.cursor()
                    row = cursor.execute(
                        "SELECT balance FROM balances WHERE utxo_id = ?", (utxo_id,)
                    ).fetchone()

                    balance = 0
                    if row:
                        balance = row["balance"]
                        total_balance += balance
                        utxo_details.append({"utxo": f"{utils.inverse_hash(txid)}:{vout}", "balance": balance})

        return {
            "address": address,
            "total_balance": total_balance,
            "utxos": utxo_details,
        }

    def get_latest_nicehashes(self, limit=50):
        """
        Return the most recent nicehashes with their rewards and block heights.

        Args:
            limit (int): Number of nicehashes to return

        Returns:
            list: List of dictionaries containing nicehash information
        """
        # Check if we can use cached result
        with self.pool.connection() as db:
            cursor = db.cursor()
            max_height = cursor.execute(
                "SELECT MAX(height) as max_height FROM blocks"
            ).fetchone()
            current_max_height = (
                max_height["max_height"]
                if max_height and max_height["max_height"] is not None
                else 0
            )

            # Use cache if available and still valid
            if (
                self._latest_nicehashes_cache is not None
                and self._latest_nicehashes_cache_block_height == current_max_height
            ):
                return self._latest_nicehashes_cache

            # Query the latest nicehashes
            nicehashes = []
            for row in cursor.execute(
                """
                SELECT height, txid, reward 
                FROM nicehashes 
                ORDER BY height DESC 
                LIMIT ?
                """,
                (limit,),
            ):
                nicehashes.append(
                    {
                        "height": row["height"],
                        "txid": row["txid"],
                        "reward": row["reward"],
                    }
                )

            # Update cache
            self._latest_nicehashes_cache = nicehashes
            self._latest_nicehashes_cache_block_height = current_max_height

            return nicehashes

    def get_stats(self):
        """
        Return various statistics about the system.

        Returns:
            dict: Statistics including RB1TS supply, nice hashes count, etc.
        """
        with self.pool.connection() as db:
            cursor = db.cursor()

            rows = cursor.execute("SELECT * FROM stats").fetchall()
            stats = {row["key"]: row["value"] for row in rows}
            stats["supply"] = int(stats["supply"])
            stats["supply_check"] = int(stats["supply_check"])
            stats["utxos_count"] = int(stats["utxos_count"])
            stats["nice_hashes_count"] = int(stats["nice_hashes_count"])
            stats["max_zero"] = int(stats["max_zero"])
            stats["last_parsed_block"] = int(stats["last_parsed_block"])

            return stats
