import copy
import json
import pytest
from unittest.mock import patch

import qa
from application import store


def test_failed_atomic_commit_preserves_file_and_runtime(tmp_path):
    original = copy.deepcopy(qa.QA_RULES)
    path = tmp_path / 'qa.json'
    with patch.object(store, 'QA_FILE', path):
        store.save_qa_rules(original)
        before = path.read_bytes()
        with patch('os.replace', side_effect=OSError('disk failure')):
            with pytest.raises(OSError):
                store.save_qa_rules(original[1:])
        assert path.read_bytes() == before
        assert qa.QA_RULES == original
        assert list(tmp_path.iterdir()) == [path]


def test_deleted_default_stays_deleted_after_reload(tmp_path):
    original = copy.deepcopy(qa.QA_RULES)
    removed = original[0]
    kept = original[1:]
    with patch.object(store, 'QA_FILE', tmp_path / 'qa.json'):
        try:
            store.save_qa_rules(kept)
            qa.QA_RULES[:] = copy.deepcopy(original)
            store.load_qa_overrides()
            assert removed.qa_id not in {r.qa_id for r in qa.QA_RULES}
            store.load_qa_overrides()
            assert removed.qa_id not in {r.qa_id for r in qa.QA_RULES}
            # A new code default must still be merged into saved overrides.
            new = copy.deepcopy(removed)
            new.qa_id = 'newdefault123'
            qa.QA_RULES.append(new)
            store.load_qa_overrides()
            assert new.qa_id in {r.qa_id for r in qa.QA_RULES}
        finally:
            qa.QA_RULES[:] = original
            qa.reindex()


def test_delete_all_and_restore_default(tmp_path):
    original = copy.deepcopy(qa.QA_RULES)
    with patch.object(store, 'QA_FILE', tmp_path / 'qa.json'):
        try:
            store.save_qa_rules([])
            qa.QA_RULES[:] = copy.deepcopy(original)
            store.load_qa_overrides()
            assert qa.QA_RULES == []
            store.save_qa_rules([original[0]])
            qa.QA_RULES[:] = copy.deepcopy(original)
            store.load_qa_overrides()
            assert [r.qa_id for r in qa.QA_RULES] == [original[0].qa_id]
            saved = json.loads(store.QA_FILE.read_text(encoding='utf-8'))
            assert original[0].qa_id not in saved['deleted_ids']
        finally:
            qa.QA_RULES[:] = original
            qa.reindex()
