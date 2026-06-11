from __future__ import annotations

import sys


if __name__ == "__main__":
    try:
        from .cli import main

        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        if not isinstance(exc, RuntimeError):
            message = f"{type(exc).__name__}: {message}"
        print(f"ERROR: {message}", file=sys.stderr)
        raise SystemExit(1)

