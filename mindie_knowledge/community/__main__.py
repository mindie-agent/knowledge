from .cli import main
from mindie_knowledge._common import configure_utf8_stdio

if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main())
