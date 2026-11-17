"""The model picker has one default and never rewrites historical entry keys."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from core.admin import model_services as ms


def configuration():
    canonical = ms.entry_name('gateway', 'deepseek')
    return {
        'selected_module': {'LLM': 'LegacyDeepSeek'},
        'model_services': {'gateway': {'name': '网关', 'base_url': 'https://gateway.test/v1',
                                     'api_key': 'catalog-private-key',
                                     'models': [{'id': 'deepseek', 'label': 'DeepSeek'}]}},
        'LLM': {
            canonical: {'type': 'pi', 'service_id': 'gateway', 'model_name': 'deepseek', 'protocol': 'chat'},
            'LegacyDeepSeek': {'type': 'pi', 'base_url': 'https://gateway.test/v1/',
                              'api_key': 'catalog-private-key', 'model_name': 'deepseek', 'protocol': 'chat'},
            'AnotherAlias': {'type': 'pi', 'service_id': 'gateway', 'model_name': 'deepseek', 'protocol': 'chat'},
        },
    }


class CatalogTests(unittest.TestCase):
    def test_active_legacy_entry_is_represented_once_without_mutation_or_secret(self):
        cfg = configuration()
        original = copy.deepcopy(cfg)
        view = ms.describe(cfg)
        self.assertEqual(view['services'][0]['models'][0]['entry'], 'LegacyDeepSeek')
        self.assertTrue(view['services'][0]['models'][0]['is_default'])
        self.assertEqual(view['existing'], [])
        self.assertEqual(view['selected'], 'LegacyDeepSeek')
        self.assertNotIn('catalog-private-key', json.dumps(view))
        self.assertEqual(cfg, original)

    def test_service_entry_wins_when_equivalent_legacy_entry_is_not_default(self):
        cfg = configuration()
        cfg['selected_module']['LLM'] = 'Unconfigured'
        view = ms.describe(cfg)
        self.assertEqual(view['services'][0]['models'][0]['entry'], ms.entry_name('gateway', 'deepseek'))
        self.assertEqual(view['existing'], [])

    def test_different_credentials_and_endpoint_are_independent_choices(self):
        cfg = configuration()
        cfg['LLM']['OtherKey'] = {**cfg['LLM']['LegacyDeepSeek'], 'api_key': 'other-private-key'}
        cfg['LLM']['OtherHost'] = {**cfg['LLM']['LegacyDeepSeek'], 'base_url': 'https://other.test/v1'}
        cfg['LLM']['Disabled'] = {**cfg['LLM']['OtherKey'], 'disabled': True}
        view = ms.describe(cfg)
        self.assertEqual({e['entry'] for e in view['existing']}, {'OtherKey', 'OtherHost'})
        self.assertNotIn('other-private-key', json.dumps(view))

    def test_probe_updates_only_canonical_and_active_equivalent(self):
        cfg = configuration()
        cfg['LLM']['OtherKey'] = {**cfg['LLM']['LegacyDeepSeek'], 'api_key': 'other-private-key'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.config.yaml'
            path.write_text(yaml.safe_dump(cfg), encoding='utf-8')
            ms.save_probe('gateway', 'deepseek', {'selected': 'responses', 'results': []}, cfg, path)
            stored = yaml.safe_load(path.read_text(encoding='utf-8'))
        self.assertEqual(stored['LLM'][ms.entry_name('gateway', 'deepseek')]['protocol'], 'responses')
        self.assertEqual(stored['LLM']['LegacyDeepSeek']['protocol'], 'responses')
        self.assertEqual(stored['LLM']['AnotherAlias']['protocol'], 'chat')
        self.assertEqual(stored['LLM']['OtherKey']['protocol'], 'chat')
        self.assertEqual(stored['selected_module'], cfg['selected_module'])
        self.assertEqual(set(stored['LLM']), set(cfg['LLM']))


if __name__ == '__main__':
    unittest.main()
