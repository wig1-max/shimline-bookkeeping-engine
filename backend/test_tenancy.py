"""One firm must not be able to read another firm's client books.

This is the test file that matters most in the console work. Every other
feature is a convenience; this one is the difference between a product an
accountant can put their name to and a disclosure incident.

The tests are written against the data layer rather than against routes on
purpose. A route guard protects the routes someone remembered to guard; a scope
that has to be composed into the query protects the query that has not been
written yet.
"""
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SHIMLINE_SECRET_KEY",
                      "test-master-key-not-used-in-production-0123456789")

import app as service  # noqa: E402
from shimline import auth, tenancy  # noqa: E402
from shimline.tenancy import OutOfScope, Scope  # noqa: E402


class TenancyCase(unittest.TestCase):
    """Two firms, one internal user, four clients."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service.DB_PATH = Path(self.temp.name) / "tenancy.db"
        self.conn = service._db()

        for ident, name in (("org_a1", "Northlake Roofing"),
                            ("org_a2", "Bayview Electric"),
                            ("org_b1", "Cedar Framing"),
                            ("org_free", "Unheld Client")):
            self.conn.execute(
                "INSERT INTO organizations(id,name,normalized_name) VALUES(?,?,?)",
                (ident, name, ident))

        tenancy.create_firm(self.conn, "firm_a", "Alder & Co")
        tenancy.create_firm(self.conn, "firm_b", "Birchwood LLP")
        tenancy.add_client(self.conn, "firm_a", "org_a1")
        tenancy.add_client(self.conn, "firm_a", "org_a2")
        tenancy.add_client(self.conn, "firm_b", "org_b1")

        self.principal_a = self._user("principal.a@example.invalid", "viewer")
        self.staff_a = self._user("staff.a@example.invalid", "viewer")
        self.principal_b = self._user("principal.b@example.invalid", "viewer")
        self.internal = self._user("ops@shimline.invalid", "operator")
        self.orphan = self._user("nobody@example.invalid", "viewer")

        tenancy.add_member(self.conn, "firm_a", self.principal_a, "principal")
        tenancy.add_member(self.conn, "firm_a", self.staff_a, "staff")
        tenancy.add_member(self.conn, "firm_b", self.principal_b, "principal")
        tenancy.assign(self.conn, "firm_a", self.staff_a, "org_a1")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _user(self, email, role):
        return auth.create_user(self.conn, email, email.split("@")[0],
                                "local-test-password-only", role)

    def _session(self, user_id, *roles):
        return {"user_id": user_id, "roles": set(roles) or {"viewer"}}

    def scope(self, user_id, *roles):
        return tenancy.scope_for(self.conn, self._session(user_id, *roles or ("viewer",)))


class WhatEachKindOfUserSees(TenancyCase):

    def test_a_principal_sees_every_client_the_firm_holds(self):
        scope = self.scope(self.principal_a)
        self.assertEqual(scope.kind, tenancy.FIRM)
        self.assertEqual(scope.organization_ids, frozenset({"org_a1", "org_a2"}))

    def test_a_staff_accountant_sees_only_their_assignments(self):
        scope = self.scope(self.staff_a)
        self.assertEqual(scope.organization_ids, frozenset({"org_a1"}))

    def test_adding_a_client_to_a_firm_does_not_grant_it_to_every_junior(self):
        """The commercial fact and the access fact are separate on purpose."""
        tenancy.add_client(self.conn, "firm_a", "org_free")
        self.assertNotIn("org_free", self.scope(self.staff_a).organization_ids)
        self.assertIn("org_free", self.scope(self.principal_a).organization_ids)

    def test_internal_staff_are_unrestricted_and_are_not_a_firm(self):
        scope = self.scope(self.internal, "operator")
        self.assertTrue(scope.unrestricted)
        self.assertIsNone(scope.firm_id)
        self.assertIsNone(scope.organization_ids)

    def test_an_internal_read_only_role_still_sees_clients(self):
        """Tenancy answers which clients; the workspace roles answer which
        actions. Confusing the two turned a 403 into a 404 once already."""
        scope = self.scope(self.orphan, "viewer")
        self.assertTrue(scope.unrestricted)

    def test_a_user_with_no_firm_and_no_workspace_role_sees_nothing(self):
        self.conn.execute("DELETE FROM user_roles WHERE user_id=?", (self.orphan,))
        self.conn.commit()
        scope = tenancy.scope_for(self.conn, {"user_id": self.orphan, "roles": set()})
        self.assertTrue(scope.sees_nothing)
        self.assertEqual(scope.organization_ids, frozenset())

    def test_no_session_sees_nothing(self):
        self.assertTrue(tenancy.scope_for(self.conn, None).sees_nothing)


class TheBoundary(TenancyCase):

    def test_one_firm_cannot_see_another_firms_client(self):
        self.assertFalse(self.scope(self.principal_a).allows("org_b1"))
        self.assertFalse(self.scope(self.principal_b).allows("org_a1"))

    def test_a_client_no_firm_holds_is_visible_to_no_firm(self):
        self.assertFalse(self.scope(self.principal_a).allows("org_free"))
        self.assertFalse(self.scope(self.principal_b).allows("org_free"))

    def test_guessing_an_id_raises_not_found_rather_than_forbidden(self):
        """403 confirms the record exists, which is an enumeration oracle.
        From outside, denial and absence must be the same answer."""
        scope = self.scope(self.principal_a)
        with self.assertRaises(OutOfScope) as caught:
            tenancy.require_organization(scope, "org_b1")
        self.assertEqual(caught.exception.status_code, 404)

        with self.assertRaises(OutOfScope) as invented:
            tenancy.require_organization(scope, "org_does_not_exist")
        self.assertEqual(invented.exception.status_code, 404)

    def test_a_firm_role_is_not_widened_by_holding_a_workspace_role(self):
        """A principal needs the reviewer role to approve anything at all.
        Reading roles first would promote every reviewing accountant to seeing
        every client in the system."""
        scope = self.scope(self.principal_a, "reviewer", "viewer")
        self.assertFalse(scope.unrestricted)
        self.assertEqual(scope.organization_ids, frozenset({"org_a1", "org_a2"}))
        self.assertFalse(scope.allows("org_b1"))

    def test_suspending_a_firm_removes_access_without_losing_history(self):
        self.conn.execute("UPDATE firms SET status='suspended' WHERE id='firm_a'")
        self.conn.commit()
        self.assertTrue(self.scope(self.principal_a).sees_nothing)
        held = self.conn.execute(
            "SELECT COUNT(*) FROM firm_clients WHERE firm_id='firm_a'").fetchone()[0]
        self.assertEqual(held, 2, "who could see these books must stay answerable")

    def test_an_assignment_to_a_client_the_firm_does_not_hold_is_refused(self):
        with self.assertRaises(ValueError):
            tenancy.assign(self.conn, "firm_a", self.staff_a, "org_b1")

    def test_an_assignment_stops_granting_access_when_the_engagement_ends(self):
        """A stale assignment row must not outlive the firm's engagement."""
        self.conn.execute(
            "DELETE FROM firm_clients WHERE firm_id='firm_a' AND organization_id='org_a1'")
        self.conn.commit()
        self.assertFalse(self.scope(self.staff_a).allows("org_a1"))

    def test_unassigning_removes_access_immediately(self):
        tenancy.unassign(self.conn, "firm_a", self.staff_a, "org_a1")
        self.conn.commit()
        self.assertTrue(self.scope(self.staff_a).sees_nothing)

    def test_a_second_firm_on_one_client_is_refused_until_it_is_meant(self):
        """Migration 020 permitted the arrangement; it must not permit the typo.

        A bookkeeper monthly and a CPA at year end is the ordinary Canadian
        arrangement, and it looks exactly like a mistyped client id. The UNIQUE
        constraint that used to catch the typo is gone, so the refusal lives in
        `add_client` where it can tell a deliberate second firm from an
        accidental one.
        """
        with self.assertRaises(ValueError) as refused:
            tenancy.add_client(self.conn, "firm_b", "org_a1")
        self.assertIn("already held by firm_a", str(refused.exception))
        self.assertFalse(self.scope(self.principal_b).allows("org_a1"),
                         "a refused add must not have written anything")

    def test_two_firms_may_hold_one_client_when_it_is_meant(self):
        tenancy.add_client(self.conn, "firm_b", "org_a1", alongside=True)
        self.conn.commit()
        self.assertTrue(self.scope(self.principal_a).allows("org_a1"))
        self.assertTrue(self.scope(self.principal_b).allows("org_a1"))

    def test_one_firm_still_cannot_hold_one_client_twice(self):
        """The composite primary key survived the rebuild."""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            tenancy.add_client(self.conn, "firm_a", "org_a1", alongside=True)

    def test_a_second_firm_does_not_widen_the_first_firms_scope(self):
        """The boundary still separates them; they merely overlap on one client."""
        tenancy.add_client(self.conn, "firm_b", "org_a1", alongside=True)
        self.conn.commit()
        self.assertEqual(self.scope(self.principal_a).organization_ids,
                         frozenset({"org_a1", "org_a2"}))
        self.assertFalse(self.scope(self.principal_a).allows("org_b1"))

    def _engagement(self, organization_id="org_a1"):
        self.conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status) "
            "VALUES('eng_d',?,'August books','in_progress')", (organization_id,))
        self.conn.commit()
        return "eng_d"

    def test_disclosing_names_the_firm_and_the_person_on_the_engagement(self):
        from shimline import crm
        engagement = self._engagement()
        crm.disclose_professional_of_record(
            self.conn, engagement, firm_id="firm_a", user_id=self.principal_a)
        row = self.conn.execute(
            "SELECT firm_id, professional_name, professional_firm_name, "
            "professional_disclosed_at FROM engagements WHERE id=?",
            (engagement,)).fetchone()
        self.assertEqual(row[0], "firm_a")
        self.assertEqual(row[2], "Alder & Co")
        self.assertTrue(row[1])
        self.assertTrue(row[3], "a disclosure has a date or it is not one")

    def test_a_firm_that_does_not_hold_the_client_cannot_be_named_on_it(self):
        """Naming an unengaged firm would put an untrue statement in front of
        the client, and it is what a mistyped firm id looks like."""
        from shimline import crm
        engagement = self._engagement()
        with self.assertRaises(ValueError) as refused:
            crm.disclose_professional_of_record(
                self.conn, engagement, firm_id="firm_b",
                user_id=self.principal_b)
        self.assertIn("does not hold this engagement's client",
                      str(refused.exception))
        self.assertIsNone(self.conn.execute(
            "SELECT professional_name FROM engagements WHERE id=?",
            (engagement,)).fetchone()[0])

    def test_an_unknown_firm_or_person_is_refused_rather_than_left_blank(self):
        from shimline import crm
        engagement = self._engagement()
        with self.assertRaises(ValueError):
            crm.disclose_professional_of_record(
                self.conn, engagement, firm_id="firm_nope",
                user_id=self.principal_a)
        with self.assertRaises(ValueError):
            crm.disclose_professional_of_record(
                self.conn, engagement, firm_id="firm_a", user_id="usr_nope")

    def test_a_second_firm_can_be_named_once_it_genuinely_holds_the_client(self):
        """The arrangement 020 exists for: the CPA joins the books the
        bookkeeper already keeps, and becomes of record for their own work."""
        from shimline import crm
        tenancy.add_client(self.conn, "firm_b", "org_a1", alongside=True)
        engagement = self._engagement()
        crm.disclose_professional_of_record(
            self.conn, engagement, firm_id="firm_b", user_id=self.principal_b)
        self.assertEqual(self.conn.execute(
            "SELECT professional_firm_name FROM engagements WHERE id=?",
            (engagement,)).fetchone()[0], "Birchwood LLP")

    def test_an_engagement_can_name_the_firm_doing_the_work(self):
        """Migration 020's other half. Nothing reads it yet -- the column exists
        so the relationship has somewhere to live before the scope derivation
        changes."""
        self.conn.execute(
            "INSERT INTO engagements(id,organization_id,title,status,firm_id) "
            "VALUES('eng_1','org_a1','August books','in_progress','firm_a')")
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT firm_id FROM engagements WHERE id='eng_1'").fetchone()[0],
            "firm_a")

    def test_a_person_cannot_belong_to_two_firms(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            tenancy.add_member(self.conn, "firm_b", self.principal_a, "staff")


class Provisioning(TenancyCase):
    """The mistake that matters is forgetting the membership, not the role."""

    def test_a_firm_user_is_confined_even_holding_a_workspace_role(self):
        user_id = tenancy.create_firm_user(
            self.conn, "firm_a", email="new.staff@example.invalid",
            display_name="New Staff", password="local-test-password-only",
            firm_role="staff", workspace_role="reviewer")
        self.conn.commit()
        scope = self.scope(user_id, "reviewer")
        self.assertEqual(scope.kind, tenancy.FIRM)
        self.assertFalse(scope.unrestricted)
        self.assertEqual(scope.organization_ids, frozenset(),
                         "a new staff member is assigned nothing until told")

    def test_a_new_principal_sees_the_firms_clients_immediately(self):
        user_id = tenancy.create_firm_user(
            self.conn, "firm_a", email="new.principal@example.invalid",
            display_name="New Principal", password="local-test-password-only",
            firm_role="principal")
        self.conn.commit()
        self.assertEqual(self.scope(user_id).organization_ids,
                         frozenset({"org_a1", "org_a2"}))

    def test_creating_into_a_missing_firm_fails_rather_than_going_internal(self):
        import sqlite3
        with self.assertRaises((sqlite3.IntegrityError, RuntimeError)):
            tenancy.create_firm_user(
                self.conn, "firm_does_not_exist", email="ghost@example.invalid",
                display_name="Ghost", password="local-test-password-only")
        self.conn.rollback()


class TheSqlFilter(TenancyCase):
    """The scope has to survive being turned into a query."""

    def _visible(self, scope):
        # The organizations table keys on `id`, so the column is named
        # explicitly. Every call site has to say which column carries the
        # organization, which is the point: there is no default to get wrong.
        clause, params = tenancy.where(scope, "id")
        return {row[0] for row in self.conn.execute(
            "SELECT id FROM organizations" + clause, params)}

    def test_an_unrestricted_scope_adds_no_clause(self):
        self.assertEqual(tenancy.sql_filter(Scope(kind=tenancy.INTERNAL)), ("", []))
        self.assertEqual(self._visible(self.scope(self.internal, "operator")),
                         {"org_a1", "org_a2", "org_b1", "org_free"})

    def test_a_firm_scope_filters_to_its_own_clients(self):
        self.assertEqual(self._visible(self.scope(self.principal_a)),
                         {"org_a1", "org_a2"})
        self.assertEqual(self._visible(self.scope(self.staff_a)), {"org_a1"})

    def test_an_empty_scope_matches_nothing_rather_than_everything(self):
        """`IN ()` is a SQL error, and the tempting fix -- drop the clause --
        turns a firm with no clients into a firm with all of them."""
        empty = self._empty_firm_principal()
        predicate, params = tenancy.sql_filter(self.scope(empty))
        self.assertEqual((predicate, params), ("1 = 0", []))
        self.assertEqual(self._visible(self.scope(empty)), set())

    def test_a_firm_holding_no_clients_sees_nothing(self):
        self.assertEqual(self._visible(self.scope(self._empty_firm_principal())), set())

    def _empty_firm_principal(self):
        tenancy.create_firm(self.conn, "firm_c", "Empty & Partners")
        lonely = self._user("solo@example.invalid", "viewer")
        tenancy.add_member(self.conn, "firm_c", lonely, "principal")
        self.conn.commit()
        return lonely

    def test_the_clause_composes_after_an_existing_where(self):
        clause, params = tenancy.and_where(self.scope(self.principal_a), "id")
        rows = {row[0] for row in self.conn.execute(
            "SELECT id FROM organizations WHERE name LIKE 'B%'" + clause, params)}
        self.assertEqual(rows, {"org_a2"})

    def test_the_filter_can_be_pointed_at_another_column(self):
        predicate, _ = tenancy.sql_filter(self.scope(self.principal_a), "r.organization_id")
        self.assertTrue(predicate.startswith("r.organization_id IN ("))


if __name__ == "__main__":
    unittest.main()
