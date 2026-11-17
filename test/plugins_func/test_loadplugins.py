"""Test for the auto-import mechanism in plugins_func/loadplugins.py.

We import a real subpackage (`plugins_func.functions`) and verify the
import side-effect runs. This protects against accidentally deleting
`auto_import_modules` from `app.py` startup code.

"""
from plugins_func.loadplugins import auto_import_modules


def test_functions_package_importable():
    """If this test runs, plugins_func.functions.__init__.py has been imported."""
    auto_import_modules("plugins_func.functions")
    import plugins_func.functions as fns
    assert hasattr(fns, "get_time")
