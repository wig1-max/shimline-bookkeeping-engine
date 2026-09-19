"""Background queue privacy boundary and synchronous fallback."""
import unittest
from unittest import mock

from pydantic import SecretStr

import app
from shimline import jobs


class _Connection:
    def __init__(self, row):
        self.row = row
        self.closed = False

    def execute(self, statement, parameters):
        self.statement = statement
        self.parameters = parameters
        return self

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


class JobTests(unittest.TestCase):
    def test_submission_task_accepts_only_an_opaque_id_then_reads_the_record(self):
        conn = _Connection(("Acme", "A Person", "person@example.invalid"))
        with mock.patch.object(jobs, "_connect", return_value=conn), \
             mock.patch.object(jobs, "send_submission_notification", return_value=True) as send:
            result = jobs.notify_submission.call_local("sub_opaque")
        self.assertTrue(result)
        self.assertEqual(conn.parameters, ("sub_opaque",))
        send.assert_called_once_with(
            "Acme", "A Person", "person@example.invalid", "sub_opaque"
        )

    def test_dispatch_enqueues_only_the_id_when_async_jobs_are_enabled(self):
        configured = app.SETTINGS.model_copy(update={
            "async_jobs_enabled": True,
            "metrics_token": SecretStr(""),
        })
        with mock.patch.object(app, "SETTINGS", configured), \
             mock.patch.object(jobs, "notify_submission") as enqueue, \
             mock.patch.object(jobs, "extract_submission_documents") as extract, \
             mock.patch.object(app, "_notify") as fallback:
            app._dispatch_submission_notification(
                "Private Co", "Private Person", "private@example.invalid", "sub_opaque"
            )
        enqueue.assert_called_once_with("sub_opaque")
        extract.assert_called_once_with("sub_opaque")
        fallback.assert_not_called()

    def test_queue_failure_falls_back_without_failing_the_intake(self):
        configured = app.SETTINGS.model_copy(update={"async_jobs_enabled": True})
        with mock.patch.object(app, "SETTINGS", configured), \
             mock.patch.object(jobs, "notify_submission", side_effect=OSError("queue down")), \
             mock.patch.object(app, "_notify") as fallback:
            app._dispatch_submission_notification("Co", "Person", "p@example.invalid", "sub")
        fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
