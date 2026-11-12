"""Generate a mock EVM holder export for bulk-mode testing.

Random 20-byte addresses in lowercase hex. Lowercase is deliberately chosen:
`analyze` rejects mixed-case addresses whose EIP-55 checksum does not match, so
lowercase keeps every row valid on its own.

The RNG is seeded, so the same 500 addresses are produced on every run.
"""

from __future__ import annotations

import random
from pathlib import Path

COUNT = 500
SEED = 42
HEADER = "address"
OUTPUT = Path(__file__).resolve().parent.parent / "data" / "mock_500_wallets.csv"


def main() -> None:
    rng = random.Random(SEED)
    addresses: set[str] = set()
    while len(addresses) < COUNT:
        addresses.add(f"0x{rng.getrandbits(160):040x}")

    rows = sorted(addresses)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8", newline="\n"
    )

    print(f"wrote {len(rows)} addresses to {OUTPUT}")


if __name__ == "__main__":
    main()
