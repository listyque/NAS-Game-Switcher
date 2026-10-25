"""Run on Linux as an ordinary user; no Docker socket or DSM installation is used."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

if os.name=='posix':
    import yaml
from server.build_dsm import DSM, postinstall_source, preinstall_source


@unittest.skipUnless(os.name=='posix', 'DSM lifecycle scripts require Linux')
class DsmInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['NGS_DSM_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.dest=self.root/'target';self.var=self.root/'var'
        self.dest.mkdir();self.var.mkdir();self.scripts=self.root/'scripts';self.scripts.mkdir()
        self.games=Path('/volume999/Files')/('ngs-test-'+uuid.uuid4().hex)
        self.games.mkdir();self.addCleanup(shutil.rmtree,self.games)
        for p in (DSM/'scripts').iterdir():
            (self.scripts/p.name).write_text(p.read_text().replace('__NGS_PUBLIC_BOOTSTRAP__',hashlib.sha256(b'{"fixture":1}').hexdigest()))
        (self.scripts/'postinst').write_text(postinstall_source())
        (self.dest/'bootstrap').mkdir();(self.dest/'bootstrap/manifest.json').write_text('{"fixture":1}')
        (self.dest/'images').mkdir();(self.dest/'images/ngs-images.tar.gz').write_bytes(b'fixture')
        (self.dest/'images/SHA256SUMS').write_text(hashlib.sha256(b'fixture').hexdigest()+'  images/ngs-images.tar.gz\n')
        (self.dest/'ui').mkdir()
        (self.dest/'client').mkdir()
        (self.dest/'public').mkdir()
        shutil.copyfile(DSM/'ui/config.template',self.dest/'ui/config.template')
        self.env=dict(os.environ,SYNOPKG_PKGNAME='NGSServer',SYNOPKG_PKGDEST=str(self.dest),SYNOPKG_PKGVAR=str(self.var),
            wizard_games=str(self.games),wizard_installers='',wizard_admin_password='A long fixture password',
            wizard_admin_confirm='A long fixture password')
        self.package=self.root/'package';self.package.mkdir()
        self.env['SYNOPKG_PKGINST_TEMP_DIR']=str(self.package)

    def prepare_payload(self,**changes):
        stage=self.package/'package'
        if stage.exists():shutil.rmtree(stage)
        stage.mkdir()
        for name in ['bootstrap','images','ui','client','public']:shutil.copytree(self.dest/name,stage/name)
        checksums='\n'.join(hashlib.sha256((stage/name).read_bytes()).hexdigest()+'  '+name
            for name in ['bootstrap/manifest.json','images/ngs-images.tar.gz'])
        (self.scripts/'preinst').write_text(preinstall_source(checksums))
        return subprocess.run(['sh',str(self.scripts/'preinst')],env=dict(self.env,**changes),stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    def run_hook(self,name,success=True,**changes):
        if name=='preinst' or (name=='postinst' and success and not (self.dest/'project/docker-compose.yaml').is_file()):
            process=self.prepare_payload(**changes)
            if process.returncode==0:
                shutil.rmtree(self.dest)
                shutil.move(self.package/'package',self.dest)
            if name=='postinst':
                self.assertEqual(process.returncode,0,process.stderr.decode())
                process=subprocess.run(['sh',str(self.scripts/name)],env=dict(self.env,**changes),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        else:
            process=subprocess.run(['sh',str(self.scripts/name)],env=dict(self.env,**changes),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.assertEqual(process.returncode==0,success,(name,process.stderr.decode()))
        self.assertNotIn(b'A long fixture password',process.stdout+process.stderr)
        return process

    def test_install_retains_data_and_password_and_uses_readonly_sources(self):
        self.run_hook('preinst');self.run_hook('postinst')
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        self.assertEqual(config['name'],'ngs-server-package')
        self.assertNotIn('build',config['services']['app'])
        mounts=config['services']['app']['volumes']
        sources=[v for v in mounts if isinstance(v,dict)]
        self.assertEqual(sources[0]['source'],str(self.games))
        self.assertTrue(all(v['read_only'] and not v['bind']['create_host_path'] for v in sources))
        self.assertEqual(set(config['services']),{'app','db','gateway'})
        self.assertEqual(config['services']['gateway']['profiles'],['public'])
        self.assertTrue(all(service['user']=='0:0' for service in config['services'].values()))
        for service in config['services'].values():
            self.assertNotIn('cpus',service, 'DSM kernels may lack CPU CFS quotas')
            self.assertIn('mem_limit',service)
        self.assertEqual(config['services']['app']['ports'],['0.0.0.0:8890:8081'])
        password=self.var/'secrets/tactic_admin_password';old=password.read_bytes()
        self.assertEqual(password.stat().st_mode&0o777,0o600)
        sentinel=self.var/'data/postgres/existing';sentinel.write_text('keep')
        self.run_hook('postinst',wizard_admin_password='A different password',wizard_admin_confirm='A different password')
        self.assertEqual(password.read_bytes(),old);self.assertEqual(sentinel.read_text(),'keep')
        for name in ['preuninst','postuninst']:self.run_hook(name)
        self.assertEqual(sentinel.read_text(),'keep')

    def test_relative_share_is_resolved_without_a_volume_number(self):
        self.run_hook('postinst',wizard_games='Files/'+self.games.name)
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        self.assertIn(str(self.games),(self.dest/'project/docker-compose.yaml').read_text())

    def test_shell_characters_are_literal_and_compose_dollars_are_escaped(self):
        source=self.games/"Game's $UNKNOWN `touch NGS_DSM_INJECTED`";source.mkdir()
        self.run_hook('postinst',wizard_games=str(source))
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        game=next(v for v in config['services']['app']['volumes'] if isinstance(v,dict) and v['target']=='/mnt/ngs-games')
        self.assertEqual(game['source'],str(source).replace('$','$$'))
        self.assertFalse(Path('NGS_DSM_INJECTED').exists())
        if os.environ.get('NGS_DSM_COMPOSE_REPORT'):
            report=Path(os.environ['NGS_DSM_COMPOSE_REPORT'])
            report.write_bytes((self.dest/'project/docker-compose.yaml').read_bytes())
            report.with_suffix('.json').write_text(json.dumps(dict(expected_source=str(source))))

    def test_ambiguous_missing_and_parent_paths_fail_before_any_write(self):
        for value in ['Files/missing-ngs-folder','../etc',str(self.games)+'/../escape','/etc',
                '/volume999oops/Files/games','/volume999/@appdata/NGSServer']:
            self.run_hook('postinst',False,wizard_games=value)
            self.assertFalse((self.var/'secrets').exists())
        self.run_hook('postinst',False,wizard_admin_confirm='wrong')
        self.assertFalse((self.var/'secrets').exists())

    def test_hidden_parent_does_not_require_package_access_for_a_readonly_bind(self):
        parent=self.games/'closed-share';source=parent/'games'
        source.mkdir(parents=True);(source/'game.exe').write_bytes(b'fixture')
        parent.chmod(0o000)
        self.addCleanup(parent.chmod,0o755)
        self.assertFalse(os.access(parent,os.X_OK), 'Run these checks as an ordinary user')
        self.assertFalse(os.path.isdir(source))
        self.run_hook('preinst',wizard_games=str(source),wizard_installers=str(source))
        self.run_hook('postinst',wizard_games=str(source),wizard_installers=str(source))
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        mounts=[v for v in config['services']['app']['volumes'] if isinstance(v,dict)]
        self.assertTrue(all(v['source']==str(source) and v['read_only'] and not v['bind']['create_host_path'] for v in mounts))
        self.assertEqual(parent.stat().st_mode&0o777,0)
        self.assertFalse(os.access(parent,os.X_OK))
        parent.chmod(0o755)
        self.assertEqual((source/'game.exe').read_bytes(),b'fixture')

    def test_visible_missing_file_and_foreign_symlink_still_fail(self):
        file=self.games/'not-a-directory';file.write_bytes(b'fixture')
        link=self.games/'foreign';link.symlink_to('/etc',target_is_directory=True)
        for source in [self.games/'missing',file,link]:
            process=self.run_hook('postinst',False,wizard_games=str(source))
            self.assertFalse((self.var/'secrets').exists())
            self.assertNotIn('Container Manager will check it'.encode(),process.stderr)

    def test_different_seed_cannot_reset_an_initialized_installation(self):
        self.run_hook('postinst');old=(self.dest/'project/docker-compose.yaml').read_bytes()
        (self.var/'data/postgres/PG_VERSION').write_text('16')
        (self.dest/'bootstrap/manifest.json').write_text('{"fixture":2}')
        self.run_hook('postinst',False)
        self.assertEqual((self.dest/'project/docker-compose.yaml').read_bytes(),old)
        self.run_hook('preupgrade',False)

    def test_worker_and_wizard_need_no_root_or_manual_docker_commands(self):
        privilege=json.loads((DSM/'conf/privilege').read_text())
        self.assertEqual(privilege,{'defaults':{'run-as':'package'}})
        worker=json.loads((DSM/'conf/resource').read_text())['docker-project']
        self.assertEqual(worker['projects'][0]['name'],'ngs-server-package')
        self.assertFalse(worker['projects'][0]['build_params']['build'])
        wizard=json.loads((DSM/'WIZARD_UIFILES/install_uifile').read_text())
        keys={s['key'] for step in wizard for item in step['items'] for s in item.get('subitems',[])}
        self.assertEqual(keys,{'wizard_port','wizard_games','wizard_installers','wizard_admin_password','wizard_admin_confirm','wizard_public_domain','wizard_http_port','wizard_https_port'})
        for script in self.scripts.iterdir():
            subprocess.run(['sh','-n',str(script)],check=True)
        self.run_hook('postinst',False,SYNOPKG_PKGNAME='other')
        self.assertFalse((self.var/'secrets').exists())

    def test_preinst_does_not_require_final_dsm_paths(self):
        self.run_hook('preinst',SYNOPKG_PKGDEST='',SYNOPKG_PKGVAR='')
        self.assertFalse((self.var/'secrets').exists())

    def test_reinstall_reads_saved_keys_before_dsm_recreates_the_var_link(self):
        secrets=self.var/'secrets';secrets.mkdir()
        (secrets/'postgres_password').write_text('a'*72)
        (secrets/'tactic_admin_password').write_text('Saved fixture password')
        alias=Path('/volume999/@appdata/NGSServer');alias.symlink_to(self.var,target_is_directory=True)
        self.addCleanup(alias.unlink)
        process=self.prepare_payload(SYNOPKG_PKGVAR='',SYNOPKG_PKGDEST_VOL='/volume999')
        self.assertEqual(process.returncode,0,process.stderr.decode())
        for key in ['postgres_password','tactic_admin_password']:
            self.assertEqual((self.package/'package/project/state/secrets'/key).read_bytes(),(secrets/key).read_bytes())

    def test_worker_inputs_exist_before_postinst_and_persist_after_it(self):
        self.run_hook('preinst',wizard_port='19347')
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        self.assertEqual(config['services']['app']['ports'],['0.0.0.0:19347:8081'])
        paths=[]
        for service in config['services'].values():
            for volume in service['volumes']:
                paths.append(Path(volume['source'] if isinstance(volume,dict) else volume.split(':')[0]))
        paths.extend(Path(value['file']) for value in config['secrets'].values())
        self.assertTrue(all(path.exists() for path in paths),[str(path) for path in paths if not path.exists()])
        self.assertFalse((self.var/'secrets').exists())
        before=(self.dest/'project/state/secrets/postgres_password').read_bytes()
        process=subprocess.run(['sh',str(self.scripts/'postinst')],env=dict(self.env,wizard_port='19347'),capture_output=True)
        self.assertEqual(process.returncode,0,process.stderr.decode())
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual((self.var/'secrets/postgres_password').read_bytes(),before)
        self.assertTrue((self.dest/'project/state/data').is_symlink())

    def test_postinst_accepts_the_preload_archive_consumed_by_dsm_worker(self):
        self.run_hook('preinst')
        (self.dest/'images/ngs-images.tar.gz').unlink()
        self.run_hook('postinst')
        self.assertTrue((self.var/'secrets/postgres_password').is_file())
        self.assertTrue((self.dest/'project/state/data').is_symlink())

    def test_preinst_uses_native_extracted_package_without_any_tgz(self):
        process=self.prepare_payload(wizard_port='19347')
        self.assertEqual(process.returncode,0,process.stderr.decode())
        self.assertFalse((self.dest/'project').exists())
        self.assertEqual(list(self.var.iterdir()),[])
        self.assertFalse(list(self.package.glob('ngs-prepare.*')))
        self.assertFalse((self.package/'package.tgz').exists())
        self.assertTrue((self.package/'package/project/docker-compose.yaml').is_file())
        self.assertEqual((self.package/'package/project/state/secrets/postgres_password').stat().st_mode&0o777,0o600)

    def test_preinst_accepts_the_payload_directory_itself_from_dsm(self):
        self.assertEqual(self.prepare_payload().returncode,0)
        stage=self.package/'package'
        shutil.rmtree(stage/'project')
        process=subprocess.run(['sh',str(self.scripts/'preinst')],
            env=dict(self.env,SYNOPKG_PKGINST_TEMP_DIR=str(stage)),capture_output=True)
        self.assertEqual(process.returncode,0,process.stderr.decode())
        self.assertTrue((stage/'project/docker-compose.yaml').is_file())
        self.assertFalse((stage/'package').exists())
        self.assertEqual(list(self.var.iterdir()),[])

    def test_preinst_refuses_missing_or_linked_payload_before_writing(self):
        self.assertEqual(self.prepare_payload().returncode,0)
        stage=self.package/'package';shutil.rmtree(stage)
        process=subprocess.run(['sh',str(self.scripts/'preinst')],env=self.env,capture_output=True)
        self.assertNotEqual(process.returncode,0)
        stage.symlink_to(self.dest,target_is_directory=True)
        process=subprocess.run(['sh',str(self.scripts/'preinst')],env=self.env,capture_output=True)
        self.assertNotEqual(process.returncode,0)
        self.assertFalse((self.dest/'project').exists())
        self.assertEqual(list(self.var.iterdir()),[])

    def test_preparation_can_repeat_and_rejects_a_changed_original(self):
        self.assertEqual(self.prepare_payload().returncode,0)
        env=dict(self.env,wizard_port='19347')
        process=subprocess.run(['sh',str(self.scripts/'preinst')],env=env,capture_output=True)
        self.assertEqual(process.returncode,0,process.stderr.decode())
        config=yaml.safe_load((self.package/'package/project/docker-compose.yaml').read_text())
        self.assertEqual(config['services']['app']['ports'],['0.0.0.0:19347:8081'])
        original=self.package/'package/images/ngs-images.tar.gz';original.write_bytes(b'damaged')
        process=subprocess.run(['sh',str(self.scripts/'preinst')],env=env,capture_output=True)
        self.assertNotEqual(process.returncode,0)
        self.assertIn('SHA256'.encode(),process.stderr)
        self.assertEqual(list(self.var.iterdir()),[])

    def test_postinst_uses_dsm_canonical_paths_when_variables_are_empty(self):
        package=Path('/var/packages/NGSServer')
        for name,target in [('target',self.dest),('var',self.var)]:
            link=package/name;link.symlink_to(target,target_is_directory=True)
            self.addCleanup(link.unlink)
        self.run_hook('postinst',SYNOPKG_PKGDEST='',SYNOPKG_PKGVAR='')
        self.assertTrue((self.dest/'project/docker-compose.yaml').is_file())

    def test_selected_port_is_shared_by_compose_and_dsm_open_button(self):
        self.run_hook('postinst',wizard_port='19347')
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        ui=json.loads((self.dest/'ui/config').read_text())['.url']['com.ngs.Server']
        self.assertEqual(config['services']['app']['ports'],['0.0.0.0:19347:8081'])
        self.assertEqual(ui['port'],'19347')
        self.assertEqual((self.var/'port').read_text().strip(),'19347')
        self.assertEqual(ui['url'],'/assets/ngs_launcher/client/')

    def test_public_mode_keeps_raw_tactic_on_loopback_and_retains_gateway_secret(self):
        options=dict(wizard_public_domain='nas.example.org',wizard_http_port='19480',wizard_https_port='19443')
        self.run_hook('postinst',**options)
        config=yaml.safe_load((self.dest/'project/docker-compose.yaml').read_text())
        self.assertEqual(config['services']['app']['ports'],['127.0.0.1:8890:8081'])
        self.assertEqual(config['services']['gateway']['profiles'],['public'])
        settings=(self.dest/'project/.env').read_text()
        self.assertIn('COMPOSE_PROFILES=public',settings)
        self.assertIn('NGS_PUBLIC_DOMAIN=nas.example.org',settings)
        self.assertEqual(len((self.var/'secrets/ngs_proxy_token').read_text().strip()),64)
        ui=json.loads((self.dest/'ui/config').read_text())['.url']['com.ngs.Server']
        self.assertEqual(ui['url'],'https://nas.example.org/assets/ngs_launcher/client/')
        self.assertNotIn('port',ui)

    def test_invalid_public_domains_or_overlapping_ports_fail_before_writes(self):
        for domain in ['https://nas.example.org','nas.example.org:443','../bad','nas.example.org;echo bad']:
            self.run_hook('preinst',False,wizard_public_domain=domain)
        self.run_hook('preinst',False,wizard_public_domain='nas.example.org',wizard_https_port='8890')
        self.assertFalse((self.var/'secrets').exists())

    def test_invalid_and_occupied_ports_fail_before_writes(self):
        import socket
        for port in ['80','65536','08890','-1','123456789000000000','8890;echo bad']:
            self.run_hook('preinst',False,wizard_port=port)
            self.run_hook('postinst',False,wizard_port=port)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            self.run_hook('postinst',False,wizard_port=str(listener.getsockname()[1]))
        self.assertFalse((self.var/'secrets').exists())

    def test_failed_preinst_can_be_repaired_without_upgrading_a_database(self):
        self.run_hook('preupgrade')
        (self.var/'data/postgres').mkdir(parents=True)
        (self.var/'.ngs-spk-seed').write_text('prepared only')
        self.run_hook('preupgrade')
        (self.var/'data/postgres/PG_VERSION').write_text('16')
        self.run_hook('preupgrade',False)

    def test_unreadable_database_cannot_be_mistaken_for_a_new_install(self):
        folder=self.var/'data/postgres';folder.mkdir(parents=True)
        folder.chmod(0o000);self.addCleanup(folder.chmod,0o700)
        self.run_hook('preupgrade',False)

    def test_public_upgrade_preserves_saved_options_and_rejects_foreign_data(self):
        self.run_hook('postinst',wizard_port='19347',wizard_public_domain='nas.example.org',wizard_http_port='19480',wizard_https_port='19443')
        (self.var/'data/postgres/PG_VERSION').write_text('16')
        fingerprint=(self.var/'.ngs-spk-seed').read_text()
        (self.var/'data/tactic/.ngs-migrated').write_text(fingerprint)
        self.run_hook('preupgrade')
        for key in list(self.env):
            if key.startswith('wizard_'):del self.env[key]
        self.run_hook('preinst');self.run_hook('postinst')
        compose=(self.dest/'project/docker-compose.yaml').read_text()
        self.assertIn('127.0.0.1:19347:8081',compose)
        self.assertIn(str(self.games),compose)
        self.assertIn('nas.example.org',(self.dest/'project/.env').read_text())
        (self.var/'data/tactic/.ngs-migrated').write_text('foreign')
        self.run_hook('preupgrade',False)


if __name__=='__main__':unittest.main()
