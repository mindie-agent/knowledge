"""Enter an owned backend in this process with bounded process-lifetime logs."""
import argparse
from importlib import metadata
import sys

from mindie_diagnostics import get_recorder
from mindie_diagnostics import capture_output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("embedding", "openviking"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    with get_recorder("mindie-knowledge").operation("knowledge.daemon." + args.service) as operation:
        with capture_output(operation):
            if args.service == "embedding":
                from .embedding import main as embedding_main
                result = embedding_main(args.arguments)
            else:
                # Invoke the distribution's own console entry, including its
                # config pre-parser. No child process or alternate PID.
                entry = next(item for item in metadata.distribution("openviking").entry_points
                             if item.group == "console_scripts" and item.name == "openviking-server")
                previous = sys.argv
                try:
                    sys.argv = ["openviking-server", *args.arguments]
                    result = entry.load()()
                finally:
                    sys.argv = previous
            if type(result) is int and result != 0:
                operation.fail("returned_failure", exit_code=result)
            return result


if __name__ == "__main__":
    raise SystemExit(main())
