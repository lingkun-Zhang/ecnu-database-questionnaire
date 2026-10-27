"""Fail-closed guards for the destructive cleanup/restore rehearsal boundary."""
from pathlib import Path
import pytest
from scripts.phase2_upgrade_rehearsal import archive_members,check_marker,validate_database,validate_output,validate_uri

@pytest.mark.parametrize('uri',[
 'mongodb://127.0.0.1:27017/?replicaSet=phase2test',
 'mongodb://example.com:27227/?replicaSet=phase2test',
 'mongodb://127.0.0.1:27227/business?replicaSet=phase2test',
 'mongodb://user:password@127.0.0.1:27227/?replicaSet=phase2test',
 'mongodb://127.0.0.1:27227/?replicaSet=other',
 'mongodb://127.0.0.1:27227,127.0.0.1:27228/?replicaSet=phase2test',
])
def test_only_explicit_credential_free_local_test_uri_is_allowed(uri):
    with pytest.raises(ValueError):validate_uri(uri)

def test_valid_test_uri_and_owned_name_are_accepted_without_connecting():
    assert validate_uri('mongodb://127.0.0.1:27227/?replicaSet=phase2test')
    assert validate_database('survey_upgrade_seed_'+'a'*32)

@pytest.mark.parametrize('name',['survey_phase1','admin','survey_upgrade_seed_'+'a'*31,'survey_upgrade_other_'+'a'*32,'survey_upgrade_mixed_'+'z'*32])
def test_business_or_lookalike_database_names_are_rejected(name):
    with pytest.raises(ValueError):validate_database(name)

@pytest.mark.parametrize('marker',[
 None,{'_id':'owner','runId':'wrong','stage':'seed'},
 {'_id':'owner','runId':'run','stage':'mixed'},
 {'_id':'owner','runId':'run','stage':'seed','extra':True},
])
def test_missing_changed_or_nonexact_marker_prevents_cleanup(marker):
    with pytest.raises(RuntimeError):check_marker(marker,'run','seed')

def test_exact_marker_is_accepted():
    check_marker({'_id':'owner','runId':'run','stage':'seed'},'run','seed')

@pytest.mark.parametrize('name',['../outside','/absolute','C:/outside','backend/../../outside','backend\\..\\outside','docs/report.md'])
def test_source_archive_cannot_escape_or_copy_unrelated_material(name):
    with pytest.raises(ValueError):archive_members([name])

def test_source_archive_allows_only_the_selected_baseline_sources():
    assert archive_members(['backend/app/main.py','requirements.txt'])==['backend/app/main.py','requirements.txt']

def test_output_must_be_new_and_stay_within_private_runtime(tmp_path):
    runtime=tmp_path/'.runtime';runtime.mkdir()
    assert validate_output(runtime/'new',tmp_path)==(runtime/'new').resolve()
    for path in [runtime,tmp_path/'outside',runtime/'..'/'escape']:
        with pytest.raises(ValueError):validate_output(path,tmp_path)
    (runtime/'existing').mkdir()
    with pytest.raises(ValueError):validate_output(runtime/'existing',tmp_path)
