import filecmp
import json
import os
from pathlib import Path
import runpy
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


mergin_stub = types.ModuleType('mergin')
mergin_stub.MerginClient = Mock()
with patch.dict('sys.modules', {'mergin': mergin_stub}):
    script = runpy.run_path(str(Path(__file__).with_name('mergin-py-sync.py')))
script['logger'].disabled = True
sync_directory = script['sync_directory']
main = script['main']


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'download'
        self.destination = self.root / 'local'
        self.source.mkdir()
        self.destination.mkdir()
        filecmp.clear_cache()

    def write_file(self, directory, name, content):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def identity(self, path):
        status = path.stat()
        return status.st_ino, status.st_mtime_ns, status.st_ctime_ns

    def test_identical_files_keep_identity_despite_different_download_timestamp(self):
        for name in ('root.txt', 'nested/data.txt'):
            local = self.write_file(self.destination, name, b'unchanged')
            self.write_file(self.source, name, b'unchanged')
            os.utime(local, ns=(1000000000, 1000000000))
        before = {name: self.identity(self.destination / name)
                  for name in ('root.txt', 'nested/data.txt')}

        sync_directory(self.source, self.destination)

        for name, identity in before.items():
            self.assertEqual(self.identity(self.destination / name), identity)

    def test_nested_change_with_equal_size_and_timestamp_updates_only_changed_file(self):
        local = self.write_file(self.destination, 'nested/changed.txt', b'old')
        incoming = self.write_file(self.source, 'nested/changed.txt', b'new')
        for path in (local, incoming):
            os.utime(path, ns=(1000000000, 1000000000))
        untouched = self.write_file(self.destination, 'nested/stable.txt', b'stable')
        self.write_file(self.source, 'nested/stable.txt', b'stable')
        before = self.identity(untouched)

        sync_directory(self.source, self.destination)

        self.assertEqual(local.read_bytes(), b'new')
        self.assertEqual(self.identity(untouched), before)

    def test_additions_and_deletions(self):
        self.write_file(self.destination, 'obsolete.txt', b'old')
        self.write_file(self.destination, 'obsolete/nested.txt', b'old')
        self.write_file(self.source, 'new/nested.txt', b'new')
        (self.source / 'empty').mkdir()

        sync_directory(self.source, self.destination)

        self.assertFalse((self.destination / 'obsolete.txt').exists())
        self.assertFalse((self.destination / 'obsolete').exists())
        self.assertEqual((self.destination / 'new/nested.txt').read_bytes(), b'new')
        self.assertTrue((self.destination / 'empty').is_dir())

    def test_file_directory_type_changes(self):
        self.write_file(self.destination, 'becomes_directory', b'old')
        self.write_file(self.source, 'becomes_directory/child.txt', b'new')
        self.write_file(self.destination, 'becomes_file/child.txt', b'old')
        self.write_file(self.source, 'becomes_file', b'new')

        sync_directory(self.source, self.destination)

        self.assertEqual((self.destination / 'becomes_directory/child.txt').read_bytes(), b'new')
        self.assertEqual((self.destination / 'becomes_file').read_bytes(), b'new')

    def test_failed_file_replacement_preserves_original(self):
        local = self.write_file(self.destination, 'data.txt', b'old')
        self.write_file(self.source, 'data.txt', b'new')
        before = self.identity(local)

        with patch('os.replace', side_effect=OSError('replacement failed')):
            with self.assertRaises(OSError):
                sync_directory(self.source, self.destination)

        self.assertEqual(local.read_bytes(), b'old')
        self.assertEqual(self.identity(local), before)

    def test_metadata_changes_do_not_touch_project_files(self):
        local = self.write_file(self.destination, 'data.txt', b'stable')
        self.write_file(self.source, 'data.txt', b'stable')
        self.write_file(self.destination, '.mergin/mergin.json', b'old')
        self.write_file(self.source, '.mergin/mergin.json', b'new')
        before = self.identity(local)

        sync_directory(self.source, self.destination)

        self.assertEqual(self.identity(local), before)
        self.assertEqual((self.destination / '.mergin/mergin.json').read_bytes(), b'new')

    def test_metadata_is_updated_after_project_files(self):
        self.write_file(self.source, '.mergin/mergin.json', b'metadata')
        self.write_file(self.source, 'data.txt', b'data')
        with patch('os.replace', wraps=os.replace) as replace:
            sync_directory(self.source, self.destination)
        self.assertEqual(Path(replace.call_args_list[-1].args[1]),
                         self.destination / '.mergin/mergin.json')

    def test_symlink_does_not_modify_external_file(self):
        external = self.write_file(self.root, 'external.txt', b'original')
        (self.destination / 'data.txt').symlink_to(external)
        self.write_file(self.source, 'data.txt', b'new')
        with self.assertRaises(ValueError):
            sync_directory(self.source, self.destination)
        self.assertEqual(external.read_bytes(), b'original')

    def run_main(self, client):
        environment = {'MERGIN_URL': 'https://example.invalid',
                       'MERGIN_USERNAME': 'test', 'MERGIN_PASSWORD': 'test',
                       'MERGIN_DATA': str(self.destination)}
        with patch.dict(os.environ, environment), patch.object(mergin_stub, 'MerginClient', return_value=client):
            return main()

    def test_first_download_then_identical_run_preserves_files_and_cleans_staging(self):
        client = Mock()
        client.projects_list.return_value = [{'namespace': 'team', 'name': 'project'}]
        client.project_info.return_value = {'id': 'project-id', 'version': 'v1'}
        staging_paths = []

        def download_project(project_path, directory, version):
            self.assertEqual(project_path, 'team/project')
            self.assertEqual(version, 'v1')
            self.assertFalse(Path(directory).exists())
            staging_paths.append(directory)
            self.write_file(Path(directory), 'nested/data.txt', b'data')
            self.write_file(Path(directory), '.mergin/mergin.json',
                            json.dumps(client.project_info.return_value).encode())

        client.download_project.side_effect = download_project
        self.assertEqual(self.run_main(client), 0)
        local = self.destination / 'team/project/nested/data.txt'
        before = self.identity(local)
        self.assertEqual(self.run_main(client), 0)
        self.assertEqual(self.identity(local), before)
        self.assertEqual(len(staging_paths), 1)
        client.download_project.assert_called_once()
        self.assertEqual(list(self.destination.glob('.mergin-sync-*')), [])

    def test_download_failure_keeps_local_files_and_continues_other_projects(self):
        local = self.write_file(self.destination, 'team/broken/data.txt', b'original')
        before = self.identity(local)
        client = Mock()
        client.projects_list.return_value = [
            {'namespace': 'team', 'name': 'broken'},
            {'namespace': 'team', 'name': 'working'},
        ]
        client.project_info.return_value = {'version': 'v1'}

        def download_project(project_path, directory, version):
            self.write_file(Path(directory), 'data.txt', b'new')
            if project_path == 'team/broken':
                raise RuntimeError('download failed')

        client.download_project.side_effect = download_project
        self.assertEqual(self.run_main(client), 1)
        self.assertEqual(local.read_bytes(), b'original')
        self.assertEqual(self.identity(local), before)
        self.assertEqual((self.destination / 'team/working/data.txt').read_bytes(), b'new')
        self.assertEqual(list(self.destination.glob('.mergin-sync-*')), [])

    def test_changed_version_syncs_without_touching_identical_files(self):
        client = Mock()
        client.projects_list.return_value = [{'namespace': 'team', 'name': 'project'}]
        client.project_info.return_value = {'id': 'project-id', 'version': 'v2'}
        project = self.destination / 'team/project'
        local = self.write_file(project, 'stable.txt', b'stable')
        self.write_file(project, 'changed.txt', b'old')
        self.write_file(project, '.mergin/mergin.json', b'{"id":"project-id","version":"v1"}')
        before = self.identity(local)

        def download_project(project_path, directory, version):
            self.assertEqual(version, 'v2')
            self.write_file(Path(directory), 'stable.txt', b'stable')
            self.write_file(Path(directory), 'changed.txt', b'new')
            self.write_file(Path(directory), '.mergin/mergin.json',
                            json.dumps(client.project_info.return_value).encode())

        client.download_project.side_effect = download_project
        self.assertEqual(self.run_main(client), 0)
        self.assertEqual(self.identity(local), before)
        self.assertEqual((project / 'changed.txt').read_bytes(), b'new')
        self.assertEqual(self.run_main(client), 0)
        client.download_project.assert_called_once()

    def test_missing_corrupt_or_mismatched_metadata_does_not_skip(self):
        remote = {'id': 'project-id', 'version': 'v2'}
        self.assertFalse(script['is_up_to_date'](self.destination, remote))
        for content in (b'invalid', b'[]', b'{}', b'{"version":"v2"}',
                        b'{"id":"other-id","version":"v2"}',
                        b'{"id":"project-id","version":"v1"}'):
            with self.subTest(content=content):
                self.write_file(self.destination, '.mergin/mergin.json', content)
                self.assertFalse(script['is_up_to_date'](self.destination, remote))

    def test_failed_sync_does_not_advance_version_and_is_retried(self):
        client = Mock()
        client.projects_list.return_value = [{'namespace': 'team', 'name': 'project'}]
        client.project_info.return_value = {'id': 'project-id', 'version': 'v2'}
        project = self.destination / 'team/project'
        self.write_file(project, 'data.txt', b'old')
        metadata = self.write_file(project, '.mergin/mergin.json',
                                   b'{"id":"project-id","version":"v1"}')

        def download_project(project_path, directory, version):
            self.write_file(Path(directory), 'data.txt', b'new')
            self.write_file(Path(directory), '.mergin/mergin.json',
                            json.dumps(client.project_info.return_value).encode())

        client.download_project.side_effect = download_project
        with patch('os.replace', side_effect=OSError('replacement failed')):
            self.assertEqual(self.run_main(client), 1)
        self.assertEqual(json.loads(metadata.read_text())['version'], 'v1')
        self.assertEqual(self.run_main(client), 0)
        self.assertEqual(client.download_project.call_count, 2)
        self.assertEqual(json.loads(metadata.read_text())['version'], 'v2')

    def test_progress_reporter_logs_and_stops_on_download_failure(self):
        client = Mock()
        event = Mock()
        event.wait.side_effect = [False, True]
        with patch('threading.Event', return_value=event), patch('threading.Thread') as thread:
            def download_project(**kwargs):
                thread.call_args.kwargs['target']()
                raise RuntimeError('download failed')

            client.download_project.side_effect = download_project
            with patch.object(script['logger'], 'info') as info:
                with self.assertRaises(RuntimeError):
                    script['download_with_progress'](client, 'team/project', str(self.source), 'v1')
            self.assertTrue(any('Still downloading team/project' in call.args[0]
                                for call in info.call_args_list))
        event.set.assert_called_once()
        thread.return_value.join.assert_called_once()


if __name__ == '__main__':
    unittest.main()