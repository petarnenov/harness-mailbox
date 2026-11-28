import hashlib
import re
import unittest
from collections import namedtuple
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'static'
VENDOR = STATIC / 'vendor'
OWN_FILES = ('index.html', 'app.js', 'theme.js', 'factory.js', 'style.css')
NAMESPACE_IDENTIFIERS = {'http://www.w3.org/2000/svg'}
REMOTE_ADDRESS = re.compile(r'^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//')
QUOTE = r'''["']'''
QUOTED = r'''(?P<quote>["'`])(?P<target>[^"'`]*)(?P=quote)'''
LOAD_PATTERNS = (
    r'\b(?:src|href|action|poster|srcset)\s*=\s*' + QUOTED,
    r'url\(\s*' + QUOTED + r'\s*\)',
    r'url\(\s*(?P<target>[^"\'`)\s]+)\s*\)',
    r'@import\s+' + QUOTED,
    r'\bfetch\(\s*' + QUOTED,
    r'\bimport\(\s*' + QUOTED,
    r'\bimport\s[^;]*?\bfrom\s+' + QUOTED,
    r'\.(?:src|href)\s*=\s*' + QUOTED,
    r'\.setAttribute\(\s*' + QUOTE + r'(?:src|href)' + QUOTE + r'\s*,\s*' + QUOTED,
    r'\.open\(\s*' + QUOTE + r'[A-Z]+' + QUOTE + r'\s*,\s*' + QUOTED,
    r'\bnew\s+(?:WebSocket|EventSource|Worker|SharedWorker|URL)\(\s*' + QUOTED,
    r'\bimportScripts\(\s*' + QUOTED,
)
STRING_LITERAL = re.compile(QUOTED)
VendorRow = namedtuple('VendorRow', 'file package version tarball_path sha256 licence')


def recorded_rows():
    """The rows of the file table in VENDOR.md; a missing table or an empty one fails the test."""
    lines = (VENDOR / 'VENDOR.md').read_text(encoding='utf-8').splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith('| File |')]
    if len(starts) != 1:
        raise AssertionError('VENDOR.md must hold exactly one file table')
    rows = []
    for line in lines[starts[0] + 2:]:
        if not line.startswith('|'):
            break
        rows.append(VendorRow(*[cell.strip().strip('`') for cell in line.strip().strip('|').split('|')]))
    if not rows:
        raise AssertionError('The file table of VENDOR.md has no row')
    return rows


def load_targets(text):
    """Every address a source file hands to the browser to load, as the load patterns find it."""
    return [match.group('target').strip() for pattern in LOAD_PATTERNS for match in re.finditer(pattern, text)]


def remote_literals(text):
    """String literals that name a remote address, except the namespace identifiers that load nothing."""
    literals = (match.group('target') for match in STRING_LITERAL.finditer(text))
    return [literal for literal in literals if REMOTE_ADDRESS.match(literal) and literal not in NAMESPACE_IDENTIFIERS]


class VendoredFileTests(unittest.TestCase):
    def test_each_vendored_file_matches_the_recorded_sha256(self):
        for row in recorded_rows():
            with self.subTest(row.file):
                self.assertRegex(row.sha256, r'^[0-9a-f]{64}$')
                self.assertEqual(row.sha256, hashlib.sha256((VENDOR / row.file).read_bytes()).hexdigest())

    def test_each_vendored_file_header_names_the_recorded_version(self):
        for row in recorded_rows():
            with self.subTest(row.file):
                text = (VENDOR / row.file).read_text(encoding='utf-8')
                self.assertTrue(row.file.endswith(f'-{row.version}.min.js'))
                if row.package == 'pixi.js':
                    self.assertIn(f'PixiJS - v{row.version}', text)
                elif row.package == 'gsap':
                    header = text[:600]
                    self.assertTrue(text.startswith('/*!'))
                    self.assertIn(f'GSAP {row.version}', header)
                    self.assertIn('https://gsap.com/standard-license', header)
                else:
                    self.fail(f'no version check for package {row.package}')

    def test_vendor_record_names_licence_and_source_of_every_file(self):
        rows, record = recorded_rows(), (VENDOR / 'VENDOR.md').read_text(encoding='utf-8')
        self.assertEqual(sorted(path.name for path in VENDOR.glob('*.min.js')), sorted(row.file for row in rows))
        for row in rows:
            with self.subTest(row.file):
                self.assertTrue(row.tarball_path.startswith('package/'))
                self.assertTrue(row.licence)
                tarball = f'https://registry.npmjs.org/{row.package}/-/{row.package}-{row.version}.tgz'
                sources = [line for line in record.splitlines() if tarball in line and re.search(r'sha512-[A-Za-z0-9+/]+={0,2}', line)]
                self.assertEqual(1, len(sources))
        licences = {row.package: row.licence for row in rows}
        self.assertIn('LICENSE-pixi.txt', licences['pixi.js'])
        self.assertIn('MIT License', (VENDOR / 'LICENSE-pixi.txt').read_text(encoding='utf-8'))
        self.assertIn('https://gsap.com/standard-license', licences['gsap'])


class OwnStaticFileTests(unittest.TestCase):
    def test_the_dashboard_own_static_files_name_no_remote_address(self):
        remote = {}
        for name in OWN_FILES:
            text = (STATIC / name).read_text(encoding='utf-8')
            names_remote = [target for target in load_targets(text) if REMOTE_ADDRESS.match(target)]
            if name.endswith('.js'):
                names_remote += remote_literals(text)
            if names_remote:
                remote[name] = names_remote
        self.assertEqual({}, remote)

    def test_the_load_patterns_find_the_local_loads_of_the_page_and_reject_a_remote_one(self):
        page = (STATIC / 'index.html').read_text(encoding='utf-8')
        self.assertTrue({'/theme.js', '/style.css', '/app.js'} <= set(load_targets(page)))
        samples = ('<script src="https://cdn.example.test/x.js"></script>', 'a { background: url(//cdn.example.test/x.png) }',
                   "fetch('http://example.test/api')", "script.src = 'https://cdn.example.test/x.js'", "import('//example.test/m.js')")
        for sample in samples:
            with self.subTest(sample):
                self.assertTrue(any(REMOTE_ADDRESS.match(target) for target in load_targets(sample)))
        self.assertEqual([], remote_literals("document.createElementNS('http://www.w3.org/2000/svg', 'svg')"))
        self.assertEqual(['https://example.test/'], remote_literals("const link = 'https://example.test/'"))


if __name__ == '__main__':
    unittest.main()
