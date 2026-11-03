"""Offline drafting CLI. Private state belongs in an explicit non-Git workdir."""
import argparse
import json
import os
from pathlib import Path
import sys
import stat
import tempfile

import debrief_coverage as C
import debrief_extract as E
import debrief_packet as P
import debrief_schema as S


BUNDLE = Path(__file__).resolve().parents[1]


def workdir(raw):
    resolved = Path(raw).resolve()
    for parent in (resolved,) + tuple(resolved.parents):
        S.need(not os.path.lexists(str(parent / '.git')),
               'workdir', 'inside a Git worktree')
    for parent in reversed((resolved,) + tuple(resolved.parents)):
        if not parent.exists():
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
    info = resolved.stat()
    S.need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
           and stat.S_IMODE(info.st_mode) == 0o700, 'workdir',
           'existing directory must be user-owned with mode 0700')
    return resolved


def write_json(path, value):
    """Replace only the named file, with private permissions from creation."""
    fd, name = tempfile.mkstemp(prefix='.debrief-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('collect', 'review', 'reproduce', 'classify', 'compile', 'show', 'recount', 'upgrade', 'downgrade'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--workdir', required=True)
        if name == 'collect':
            cmd.add_argument('--root', action='append', required=True, metavar='ROLE=PATH')
        if name in ('review', 'reproduce'):
            cmd.add_argument('--candidate', type=int, required=True)
        if name == 'review':
            cmd.add_argument('--disposition', choices=('extends', 'conflicts'), required=True)
            cmd.add_argument('--rationale-file', required=True)
        if name == 'reproduce':
            for field in ('receipt-ref', 'tested-version', 'probe-file', 'observed-file'):
                cmd.add_argument('--' + field, required=True)
        if name == 'classify':
            cmd.add_argument('--checkout', required=True)
    args = parser.parse_args(argv)
    try:
        catalog = S.load_catalog(BUNDLE / 'catalog.json')
        directory = workdir(args.workdir)
        dossier_path, packet_path = directory / 'dossier.json', directory / 'packet.json'
        if args.command == 'collect':
            artifacts = {'dossier.json', 'packet.json', 'approval.json',
                         'filing.json', 'gh-receipt.json'}
            S.need(not any(p.name in artifacts or p.name.startswith(
                ('.debrief-', '.filing-', '.gh-receipt-')) for p in directory.iterdir()),
                'collect', 'debrief artifact already exists; use a fresh workdir')
            roots = []
            for index, raw in enumerate(args.root, 1):
                role, sep, path = raw.partition('=')
                S.need(bool(sep and path), 'root', 'expected ROLE=PATH')
                roots.append(dict(label='root %d' % index, role=role, path=path))
            dossier = E.collect(roots)
            dossier.update(schema_version=S.DOSSIER_SCHEMA, candidates=[])
        else:
            dossier = P.load_dossier(dossier_path, catalog)
        if args.command == 'downgrade':
            raise S.SchemaError('downgrade: refused; v1 is frozen')
        if args.command == 'upgrade':
            dossier['schema_version'] = S.DOSSIER_SCHEMA
        if args.command == 'review':
            C.record_review(dossier, args.candidate, args.disposition,
                            Path(args.rationale_file).read_text(encoding='utf-8'), catalog)
        elif args.command == 'reproduce':
            C.record_reproduction(dossier, args.candidate, args.receipt_ref,
                                  args.tested_version,
                                  Path(args.probe_file).read_text(encoding='utf-8'),
                                  Path(args.observed_file).read_text(encoding='utf-8'), catalog)
        elif args.command == 'classify':
            dossier['candidates'] = C.group(dossier['candidates'], dossier['cohorts'], catalog,
                                             schema=dossier['schema_version'])
            registry = S.load_json(BUNDLE / 'coverage.json')
            for candidate in dossier['candidates']:
                if candidate['coverage_disposition'] not in ('extends', 'conflicts'):
                    candidate['coverage_disposition'] = C.classify(
                        candidate, registry, args.checkout,
                        provenance=dossier['provenance'], catalog=catalog)
            C.select(dossier, catalog)
        elif args.command == 'compile':
            write_json(packet_path, P.compile_packet(dossier, catalog))
        elif args.command in ('show', 'recount'):
            packet = P.load_packet(packet_path, catalog)
            P.recount(dossier, packet, catalog)
            if args.command == 'show':
                title, body = P.render(packet, catalog)
                print('TITLE\n' + title + '\nBODY\n' + body)
                print('APPROVAL DIGEST\n' + P.approval_digest(packet, title))
                print('PRIVATE SUMMARY (never upload or paste)')
                print(json.dumps(dict(omissions=dossier['omissions'], candidates=[
                    dict(id=i, status=c['status'], reason=c['rationale'])
                    for i, c in enumerate(dossier['candidates']) if c['status'] != 'selected'
                ]), ensure_ascii=True, indent=2))
            else:
                print('recount valid')
        if args.command in ('collect', 'review', 'reproduce', 'classify', 'upgrade'):
            P.validate_dossier(dossier, catalog)
            write_json(dossier_path, dossier)
    except (OSError, ValueError, RecursionError) as exc:
        print('debrief failed: %s' % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
