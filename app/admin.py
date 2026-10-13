"""Local-only administrator account bootstrap and recovery commands."""
import argparse
import getpass
import sys

try:
    from app import api, security
except ImportError:  # pragma: no cover
    import api
    import security


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage StormPilot administrator accounts.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    bootstrap = subparsers.add_parser('bootstrap', help='Create the first administrator; only works before any account exists.')
    bootstrap.add_argument('--username', required=True)
    bootstrap.add_argument('--password-stdin', action='store_true', help='Read the password from standard input instead of prompting.')
    args = parser.parse_args(argv)

    api.initialize_storage()
    if args.command == 'bootstrap':
        password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('New administrator password (12+ characters): ')
        if not args.password_stdin:
            confirmation = getpass.getpass('Confirm password: ')
            if password != confirmation:
                parser.error('Passwords do not match.')
        status, result = security.bootstrap_admin(args.username, password)
        if status != 201:
            parser.error(result['error'])
        print(f"Created initial administrator {result['username']} ({result['user_id']}).")
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
