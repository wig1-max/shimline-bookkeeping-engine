"""The operator path into firm tenancy.

Until something can create a firm, the boundary is code nobody can reach. These
tests drive the script the way an operator would, because a provisioning tool
that is wrong is indistinguishable from having no boundary at all.
"""
import importlib.util
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import tenancy  # noqa: E402

SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "firm_admin.py")
PASSWORD = "local-test-password-only"


def _load():
    spec = importlib.util.spec_from_file_location("firm_admin", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FirmAdminCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "admin.db"
        conn = service._db()
        for ident, name in (("org_1", "Northlake Roofing"),
                            ("org_2", "Bayview Electric")):
            conn.execute(
                "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                (ident, name, ident))
        conn.commit()
        conn.close()
        self.module = _load()

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, *argv, password=PASSWORD):
        out = StringIO()
        with mock.patch("sys.stdout", out), \
             mock.patch.object(self.module.getpass, "getpass",
                               return_value=password):
            code = self.module.main(["--db", str(service.DB_PATH), *argv])
        return code, out.getvalue()

    def firm_id(self):
        conn = service._db()
        row = conn.execute("SELECT id FROM firms LIMIT 1").fetchone()
        conn.close()
        return row[0]

    def scope_of(self, email):
        conn = service._db()
        row = conn.execute(
            "SELECT u.id, GROUP_CONCAT(ur.role_id) FROM users u "
            "LEFT JOIN user_roles ur ON ur.user_id = u.id "
            "WHERE u.email = ? GROUP BY u.id", (email,)).fetchone()
        scope = tenancy.scope_for(
            conn, {"user_id": row[0], "roles": set((row[1] or "").split(","))})
        conn.close()
        return scope


class TheHappyPath(FirmAdminCase):

    def test_a_firm_a_client_and_a_principal_can_be_stood_up(self):
        code, out = self.run_command("create-firm", "Alder & Co")
        self.assertEqual(code, 0)
        self.assertIn("Alder & Co", out)

        firm = self.firm_id()
        self.run_command("add-client", firm, "org_1")
        self.run_command("add-person", firm, "dana@alder.ca", "Dana Alder",
                         "--role", "principal", "--workspace-role", "reviewer")

        scope = self.scope_of("dana@alder.ca")
        self.assertEqual(scope.kind, tenancy.FIRM)
        self.assertEqual(scope.organization_ids, frozenset({"org_1"}))

    def test_a_new_staff_member_can_open_nothing_until_assigned(self):
        self.run_command("create-firm", "Alder & Co")
        firm = self.firm_id()
        self.run_command("add-client", firm, "org_1")
        code, out = self.run_command("add-person", firm, "sam@alder.ca", "Sam")
        self.assertIn("can open nothing yet", out)
        self.assertTrue(self.scope_of("sam@alder.ca").sees_nothing)

        conn = service._db()
        user_id = conn.execute("SELECT id FROM users WHERE email='sam@alder.ca'"
                               ).fetchone()[0]
        conn.close()
        self.run_command("assign", firm, user_id, "org_1")
        self.assertEqual(self.scope_of("sam@alder.ca").organization_ids,
                         frozenset({"org_1"}))

        self.run_command("unassign", firm, user_id, "org_1")
        self.assertTrue(self.scope_of("sam@alder.ca").sees_nothing)

    def test_suspending_removes_access_and_keeps_the_history(self):
        self.run_command("create-firm", "Alder & Co")
        firm = self.firm_id()
        self.run_command("add-client", firm, "org_1")
        self.run_command("add-person", firm, "dana@alder.ca", "Dana",
                         "--role", "principal")
        self.run_command("suspend", firm)
        self.assertTrue(self.scope_of("dana@alder.ca").sees_nothing)

        conn = service._db()
        held = conn.execute(
            "SELECT COUNT(*) FROM firm_clients WHERE firm_id=?", (firm,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(held, 1)

        self.run_command("reinstate", firm)
        self.assertEqual(self.scope_of("dana@alder.ca").organization_ids,
                         frozenset({"org_1"}))

    def test_show_says_what_each_person_can_open(self):
        self.run_command("create-firm", "Alder & Co")
        firm = self.firm_id()
        self.run_command("add-client", firm, "org_1")
        self.run_command("add-client", firm, "org_2")
        self.run_command("add-person", firm, "dana@alder.ca", "Dana",
                         "--role", "principal")
        self.run_command("add-person", firm, "sam@alder.ca", "Sam")
        _, out = self.run_command("show", firm)
        self.assertIn("every client the firm holds (2)", out)
        self.assertIn("0 assigned client(s)", out)


class TheRefusals(FirmAdminCase):

    def test_a_client_no_firm_holds_cannot_be_assigned(self):
        self.run_command("create-firm", "Alder & Co")
        firm = self.firm_id()
        self.run_command("add-person", firm, "sam@alder.ca", "Sam")
        conn = service._db()
        user_id = conn.execute("SELECT id FROM users WHERE email='sam@alder.ca'"
                               ).fetchone()[0]
        conn.close()
        with self.assertRaises(ValueError):
            self.run_command("assign", firm, user_id, "org_1")

    def test_a_client_that_does_not_exist_is_refused(self):
        self.run_command("create-firm", "Alder & Co")
        with self.assertRaises(SystemExit):
            self.run_command("add-client", self.firm_id(), "org_nope")

    def test_a_short_password_is_refused(self):
        self.run_command("create-firm", "Alder & Co")
        with self.assertRaises(SystemExit) as caught:
            self.run_command("add-person", self.firm_id(), "x@alder.ca", "X",
                             password="short")
        self.assertIn("12 characters", str(caught.exception))

    def test_a_missing_database_is_named_rather_than_created(self):
        """Creating an empty one silently would put a firm somewhere nobody
        is looking, and the operator would believe it had worked."""
        with self.assertRaises(SystemExit) as caught:
            self.module.main(["--db", str(Path(self.temp.name) / "nope.db"),
                              "list"])
        self.assertIn("No database at", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
