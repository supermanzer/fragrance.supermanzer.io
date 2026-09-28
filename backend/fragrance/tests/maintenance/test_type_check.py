"""
Enforces the ty type-check gate on the Stripe-calling modules (billing.py,
views.py, tasks.py, configure_stripe_portal.py). See [tool.ty] in
pyproject.toml for how the check is scoped to just those four files, and why
(Django's untyped ORM would otherwise drown the Stripe-params signal this
checker exists to catch).

ty (https://docs.astral.sh/ty/) is a CLI with no Python API, so this runs
`ty check` as a subprocess -- the same way a developer or CI would invoke it.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

# fragrance/tests/maintenance/test_type_check.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parents[3]

STRIPE_CALLING_MODULES = [
    "fragrance/billing.py",
    "fragrance/views.py",
    "fragrance/tasks.py",
    "fragrance/management/commands/configure_stripe_portal.py",
]


def _ty_executable() -> str:
    """
    Resolved the same way manage.py's own interpreter is: sys.executable's
    bin dir first (the venv this test process is actually running in),
    falling back to PATH -- so this doesn't hard-code a venv layout.
    """
    candidate = Path(sys.executable).with_name("ty")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("ty")
    if found is not None:
        return found
    raise AssertionError(
        "ty is not runnable in this environment. On the host: cd backend && "
        "uv add --dev ty. Then confirm the backend-deps sentinel synced it: "
        "docker compose logs backend-deps --since 2m."
    )


class StripeParamsTypeCheckTests(SimpleTestCase):
    """
    Three tests, each proving something the others can't:

    - test_ty_check_passes_on_the_stripe_calling_modules is the actual gate:
      it fails the suite if a real stripe-python *Params TypedDict mistake,
      a Stripe SDK response-attribute typo, or any other type error ty's
      [tool.ty] configuration doesn't deliberately suppress is introduced
      into these four files.
    - test_ty_actually_catches_a_wrong_stripe_params_key is an anti-vacuous
      control for the params-shape class of error: without it, a
      misconfigured or silently-broken checker (e.g. one that always exits
      0) would make the gate above pass for the wrong reason, the same
      failure mode stripe_fakes.py's own `_guard` mechanism exists to catch
      for the mocked Stripe client. It runs in total isolation from this
      project's real [tool.ty] configuration, so it proves ty itself still
      flags the mistake, not that some override happens to let it through.
    - test_ty_actually_catches_a_stripe_response_attribute_typo_under_the_real_config
      is the anti-vacuous control for the unresolved-attribute class of
      error specifically, and -- unlike the params-key control above -- it
      runs AGAINST this project's real pyproject.toml, copied verbatim, at
      each of the four real module paths. unresolved-attribute is a rule
      [tool.ty] used to suppress file-wide for these four modules (now
      suppressed per-line instead; see [tool.ty] in pyproject.toml), so a
      control that ran in total isolation like the params-key one would
      prove nothing about whether *this project's own config* still lets a
      genuine Stripe SDK attribute typo through.
    """

    def test_ty_check_passes_on_the_stripe_calling_modules(self) -> None:
        result = subprocess.run(
            [_ty_executable(), "check", *STRIPE_CALLING_MODULES],
            cwd=BACKEND_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            "ty check found type errors in the Stripe-calling modules "
            f"(exit {result.returncode}):\n{result.stdout}\n{result.stderr}",
        )

    def test_ty_actually_catches_a_wrong_stripe_params_key(self) -> None:
        """
        A `params={...}` call with a typo'd key, checked in total isolation
        from this project's pyproject.toml (a throwaway empty ty.toml via
        --config-file, in a tempdir outside backend/) so this proves ty
        itself still flags the mistake -- not that some override in our real
        [tool.ty] configuration happens to let it through.
        """
        snippet = (
            "import stripe\n"
            "from stripe.params import CustomerCreateParams\n"
            "\n"
            "\n"
            "def bad(client: stripe.StripeClient) -> None:\n"
            "    client.v1.customers.create(\n"
            '        params=CustomerCreateParams(emial="wrong-key@example.test")\n'
            "    )\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "ty.toml").write_text("")
            snippet_path = tmp_path / "bad_params_snippet.py"
            snippet_path.write_text(snippet)

            result = subprocess.run(
                [
                    _ty_executable(),
                    "check",
                    str(snippet_path),
                    "--config-file",
                    str(tmp_path / "ty.toml"),
                    "--python",
                    str(BACKEND_ROOT / ".venv"),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertNotEqual(
            result.returncode,
            0,
            "ty did not flag a wrong Stripe params key -- the enforcement "
            f"test above would pass vacuously:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("emial", result.stdout)

    def test_ty_actually_catches_a_stripe_response_attribute_typo_under_the_real_config(
        self,
    ) -> None:
        """
        unresolved-attribute used to be disabled file-wide for these four
        modules; it is now suppressed only at the ~82 individually reviewed
        Django-ORM sites (see [tool.ty] in pyproject.toml). This proves that
        gap is actually closed under the REAL project config, not just in
        theory: it copies backend/pyproject.toml verbatim into a throwaway
        tempdir, overwrites each of the four real module paths with a
        two-line function carrying a genuine Stripe SDK attribute typo
        (`session.urlxyz` on a `stripe.checkout.Session`-typed parameter --
        the same class of mistake `create_checkout_session`'s real
        `session.url` access would be), and runs bare `ty check` (relying on
        [tool.ty.src].include, exactly like a developer running it with no
        arguments would) from that tempdir.

        Unlike test_ty_actually_catches_a_wrong_stripe_params_key above,
        this deliberately does NOT run in isolation from the real config:
        an isolated run would still pass if the file-wide override were
        silently reintroduced, since [tool.ty.overrides] lives only in the
        real pyproject.toml this test copies. If this test is run while
        that override is still present, it fails -- that failure was
        captured once, by hand, before the override was removed, as the
        mutation proof for this control.
        """
        snippet = (
            "import stripe\n"
            "\n"
            "\n"
            "def bad(session: stripe.checkout.Session) -> None:\n"
            "    session.urlxyz\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            shutil.copyfile(BACKEND_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
            for relative_module in STRIPE_CALLING_MODULES:
                module_path = tmp_path / relative_module
                module_path.parent.mkdir(parents=True, exist_ok=True)
                module_path.write_text(snippet)

            result = subprocess.run(
                [
                    _ty_executable(),
                    "check",
                    "--output-format",
                    "concise",
                    "--python",
                    str(BACKEND_ROOT / ".venv"),
                ],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertNotEqual(
            result.returncode,
            0,
            "ty did not flag a Stripe response-attribute typo under the real "
            f"project [tool.ty] config -- the per-line suppressions have "
            f"regressed into a file-wide gap:\n{result.stdout}\n{result.stderr}",
        )
        # Concise output is one line per diagnostic
        # (path:line:col: error[rule] message), so each module contributes
        # exactly one line containing both its own path and "urlxyz".
        diagnostic_lines = [line for line in result.stdout.splitlines() if "urlxyz" in line]
        for relative_module in STRIPE_CALLING_MODULES:
            matching = [line for line in diagnostic_lines if line.startswith(relative_module + ":")]
            self.assertEqual(
                len(matching),
                1,
                f"expected exactly one urlxyz diagnostic for {relative_module}:\n{result.stdout}",
            )
            self.assertIn("unresolved-attribute", matching[0])
        self.assertEqual(
            len(diagnostic_lines),
            len(STRIPE_CALLING_MODULES),
            f"expected exactly one urlxyz diagnostic per module, no more:\n{result.stdout}",
        )
