"""Phase 0 regression tests: symlink-resolving workspace containment.

The Phase 0 security fix (docs/sandbox-design.md §1 gap table) makes the
workspace boundary resolve symlinks BEFORE deciding containment, closing
the mismatch where write-time clamping used ``normcase(abspath)`` while
artifact registration used ``realpath`` — a symlink planted inside a task
scope could redirect reads/writes outside the workspace root.

Covers: normal paths, nested dirs, ``..`` traversal, absolute paths,
symlinks (to inside and outside), nested symlink escapes, missing/new
paths, tool-level checks, adapter clamps, and artifact registration's
independent containment refusal.
"""

import os
import tempfile

import pytest

from services.security.path_containment import (
    contained_path,
    is_contained,
    realpath_norm,
)


def _can_symlink() -> bool:
    """Probe whether this host permits symlink creation (Windows needs
    privileges/developer mode; tests skip honestly when it does not)."""
    base = tempfile.mkdtemp(prefix="raivisox_linkprobe_")
    try:
        src = os.path.join(base, "probe_src")
        with open(src, "w", encoding="utf-8") as f:
            f.write("x")
        link = os.path.join(base, "probe_link")
        os.symlink(src, link)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)


_SYMLINKS_OK = _can_symlink()

needs_symlinks = pytest.mark.skipif(
    not _SYMLINKS_OK,
    reason="symlink creation not permitted on this host/OS",
)


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


class TestPathContainmentHelper:
    def test_normal_file_inside_root(self, tmp_path):
        target = tmp_path / "deliverables" / "result.md"
        target.parent.mkdir()
        target.write_text("ok")
        assert is_contained(str(target), str(tmp_path)) is True
        assert contained_path(str(target), str(tmp_path)) == str(target)

    def test_nested_directories_inside_root(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "file.txt"
        assert contained_path(str(deep), str(tmp_path)) == str(deep)

    def test_dotdot_escape_rejected(self, tmp_path):
        assert contained_path(str(tmp_path / ".." / "elsewhere"), str(tmp_path)) is None
        rel = os.path.join("sub", "..", "..", "escape.txt")
        assert contained_path(rel, str(tmp_path)) is None

    def test_absolute_outside_rejected(self, tmp_path):
        outside = tempfile.mkdtemp(prefix="raivisox_outside_")
        assert contained_path(os.path.join(outside, "f.txt"), str(tmp_path)) is None

    def test_prefix_lookalike_directory_rejected(self, tmp_path):
        sibling = tmp_path.with_name(tmp_path.name + "-evil")
        sibling.mkdir()
        assert contained_path(str(sibling / "f.txt"), str(tmp_path)) is None

    def test_root_itself_is_contained(self, tmp_path):
        assert contained_path(str(tmp_path), str(tmp_path)) == str(tmp_path)

    def test_missing_new_path_over_existing_dirs_allowed(self, tmp_path):
        (tmp_path / "exists").mkdir()
        new_file = tmp_path / "exists" / "new.txt"
        assert contained_path(str(new_file), str(tmp_path)) == str(new_file)

    def test_missing_path_with_outside_existing_parents_rejected(self, tmp_path):
        outside = tempfile.mkdtemp(prefix="raivisox_outside2_")
        assert contained_path(os.path.join(outside, "new.txt"), str(tmp_path)) is None

    def test_empty_inputs_rejected(self, tmp_path):
        assert contained_path("", str(tmp_path)) is None
        assert contained_path(str(tmp_path / "f.txt"), "") is None

    @needs_symlinks
    def test_symlink_to_outside_rejected(self, tmp_path):
        outside_file = os.path.join(
            tempfile.mkdtemp(prefix="raivisox_outside3_"), "secret.txt"
        )
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("secret")
        link = tmp_path / "escape_link"
        link.symlink_to(outside_file)
        assert contained_path(str(link), str(tmp_path)) is None

    @needs_symlinks
    def test_nested_symlink_escape_rejected(self, tmp_path):
        outside_dir = tempfile.mkdtemp(prefix="raivisox_outside4_")
        inner = tmp_path / "inner"
        inner.mkdir()
        link = inner / "up"
        link.symlink_to(outside_dir)
        assert contained_path(str(link / "payload.txt"), str(tmp_path)) is None

    @needs_symlinks
    def test_symlink_chain_to_outside_rejected(self, tmp_path):
        outside_file = os.path.join(
            tempfile.mkdtemp(prefix="raivisox_outside5_"), "secret.txt"
        )
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("secret")
        first = tmp_path / "l1"
        first.symlink_to(outside_file)
        second = tmp_path / "l2"
        second.symlink_to(first)
        assert contained_path(str(second), str(tmp_path)) is None

    @needs_symlinks
    def test_legitimate_in_workspace_symlink_allowed(self, tmp_path):
        """Documented behavior: a link whose REAL resolution stays inside
        the root is allowed; containment judges the resolution, not the
        link path."""
        target = tmp_path / "real_target.txt"
        link = tmp_path / "alias.txt"
        link.symlink_to(target)
        assert contained_path(str(link), str(tmp_path)) == str(link)

    def test_realpath_norm_resolves_and_normalizes(self, tmp_path):
        deep = tmp_path / "x" / ".." / "y"
        assert realpath_norm(str(deep)) == realpath_norm(str(tmp_path / "y"))
        assert realpath_norm(str(tmp_path)) == os.path.normcase(
            os.path.realpath(str(tmp_path))
        )


# ---------------------------------------------------------------------------
# Tool-level use-time checks (file_reader / file_writer)
# ---------------------------------------------------------------------------


class TestFileToolContainment:
    def _writer(self, root):
        from integrations.tools.file_writer import FileWriterTool

        return FileWriterTool(allowed_directories=[str(root)])

    def _reader(self, root):
        from integrations.tools.file_reader import FileReaderTool

        return FileReaderTool(allowed_directories=[str(root)])

    async def test_write_normal_and_read_back(self, tmp_path):
        w = self._writer(tmp_path)
        res = await w.execute({"path": str(tmp_path / "out" / "f.txt"), "content": "data"})
        assert res.success, res.error
        r = self._reader(tmp_path)
        res = await r.execute({"path": str(tmp_path / "out" / "f.txt")})
        assert res.success and res.output["content"] == "data"

    async def test_write_dotdot_escape_denied(self, tmp_path):
        res = await self._writer(tmp_path).execute(
            {"path": str(tmp_path / ".." / "escaped.txt"), "content": "x"}
        )
        assert not res.success and "Access denied" in (res.error or "")

    async def test_write_absolute_outside_denied(self, tmp_path):
        outside = tempfile.mkdtemp(prefix="raivisox_outside6_")
        res = await self._writer(tmp_path).execute(
            {"path": os.path.join(outside, "f.txt"), "content": "x"}
        )
        assert not res.success and "Access denied" in (res.error or "")

    async def test_read_absolute_outside_denied(self, tmp_path):
        outside = tempfile.mkdtemp(prefix="raivisox_outside7_")
        p = os.path.join(outside, "f.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("secret")
        res = await self._reader(tmp_path).execute({"path": p})
        assert not res.success and "not in allowed directories" in (res.error or "")

    @needs_symlinks
    async def test_write_through_outside_symlink_denied(self, tmp_path):
        outside_file = os.path.join(
            tempfile.mkdtemp(prefix="raivisox_outside8_"), "victim.txt"
        )
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("original")
        link = tmp_path / "door.txt"
        link.symlink_to(outside_file)
        res = await self._writer(tmp_path).execute(
            {"path": str(link), "content": "pwned"}
        )
        assert not res.success and "Access denied" in (res.error or "")
        with open(outside_file, "r", encoding="utf-8") as f:
            assert f.read() == "original"

    @needs_symlinks
    async def test_read_through_outside_symlink_denied(self, tmp_path):
        outside_file = os.path.join(
            tempfile.mkdtemp(prefix="raivisox_outside9_"), "victim.txt"
        )
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("secret")
        link = tmp_path / "window.txt"
        link.symlink_to(outside_file)
        res = await self._reader(tmp_path).execute({"path": str(link)})
        assert not res.success and "not in allowed directories" in (res.error or "")

    @needs_symlinks
    async def test_in_workspace_symlink_write_lands_inside(self, tmp_path):
        """Legitimate link: allowed, and the resulting file's real
        resolution stays inside the workspace root."""
        link = tmp_path / "alias.txt"
        link.symlink_to(tmp_path / "real_target.txt")
        res = await self._writer(tmp_path).execute(
            {"path": str(link), "content": "fine"}
        )
        assert res.success, res.error
        written = realpath_norm(str(link))
        assert is_contained(written, str(tmp_path))
        with open(written, "r", encoding="utf-8") as f:
            assert f.read() == "fine"

    @needs_symlinks
    async def test_directory_symlink_escape_denied(self, tmp_path):
        outside_dir = tempfile.mkdtemp(prefix="raivisox_outside10_")
        linkdir = tmp_path / "linkdir"
        linkdir.symlink_to(outside_dir)
        res = await self._writer(tmp_path).execute(
            {"path": str(linkdir / "f.txt"), "content": "x"}
        )
        assert not res.success and "Access denied" in (res.error or "")
        assert not os.path.exists(os.path.join(outside_dir, "f.txt"))


# ---------------------------------------------------------------------------
# Adapter clamps (builtin fs adapters, finalize, codegen)
# ---------------------------------------------------------------------------


def _ctx(task_id=None):
    from integrations.adapters.base import AdapterContext

    return AdapterContext(actor_id="test-actor", task_id=task_id)


class TestAdapterClampContainment:
    async def test_fs_write_normal_task_scoped_path_allowed(self, tmp_path):
        from integrations.adapters.builtin import FileWriteAdapter

        adapter = FileWriteAdapter(str(tmp_path))
        res = await adapter.run(
            {"path": "deliverables/result.md", "content": "ok"}, _ctx("task-1")
        )
        assert res.success, res.error
        expected = os.path.join(str(tmp_path), "task-1", "deliverables", "result.md")
        assert os.path.isfile(expected)
        assert is_contained(expected, str(tmp_path / "task-1"))

    async def test_fs_write_dotdot_escape_denied_and_audited(self, tmp_path):
        from integrations.adapters.builtin import FileWriteAdapter
        from services.security.audit_trail import AuditTrail

        before = len(AuditTrail.instance()._events)
        adapter = FileWriteAdapter(str(tmp_path))
        res = await adapter.run({"path": "../escape.txt", "content": "x"}, _ctx("task-1"))
        assert not res.success and "Access denied" in (res.error or "")
        assert len(AuditTrail.instance()._events) > before

    async def test_fs_write_absolute_outside_denied(self, tmp_path):
        from integrations.adapters.builtin import FileWriteAdapter

        outside = tempfile.mkdtemp(prefix="raivisox_outside11_")
        adapter = FileWriteAdapter(str(tmp_path))
        res = await adapter.run(
            {"path": os.path.join(outside, "f.txt"), "content": "x"}, _ctx("task-1")
        )
        assert not res.success and "Access denied" in (res.error or "")

    async def test_fs_read_cross_task_absolute_denied(self, tmp_path):
        from integrations.adapters.builtin import FileReadAdapter

        other = tmp_path / "task-2"
        other.mkdir()
        (other / "secret.txt").write_text("sibling")
        adapter = FileReadAdapter(str(tmp_path))
        res = await adapter.run(
            {"path": str(other / "secret.txt")}, _ctx("task-1")
        )
        assert not res.success and "Access denied" in (res.error or "")

    @needs_symlinks
    async def test_fs_write_symlink_escape_denied(self, tmp_path):
        from integrations.adapters.builtin import FileWriteAdapter

        outside_file = os.path.join(
            tempfile.mkdtemp(prefix="raivisox_outside12_"), "victim.txt"
        )
        with open(outside_file, "w", encoding="utf-8") as f:
            f.write("original")
        scope = tmp_path / "task-1"
        scope.mkdir(parents=True, exist_ok=True)
        link = scope / "door.txt"
        link.symlink_to(outside_file)
        adapter = FileWriteAdapter(str(tmp_path))
        res = await adapter.run({"path": "door.txt", "content": "pwned"}, _ctx("task-1"))
        assert not res.success and "Access denied" in (res.error or "")
        with open(outside_file, "r", encoding="utf-8") as f:
            assert f.read() == "original"

    async def test_finalize_dotdot_escape_denied(self, tmp_path):
        from integrations.adapters.finalize import FinalizeAdapter

        adapter = FinalizeAdapter(str(tmp_path))
        res = await adapter.run(
            {"document": "doc", "path": "../escape.md"}, _ctx("task-1")
        )
        assert not res.success and "Access denied" in (res.error or "")

    async def test_finalize_absolute_into_other_task_denied(self, tmp_path):
        from integrations.adapters.finalize import FinalizeAdapter

        other = tmp_path / "task-2"
        other.mkdir()
        adapter = FinalizeAdapter(str(tmp_path))
        res = await adapter.run(
            {"document": "doc", "path": str(other / "x.md")}, _ctx("task-1")
        )
        assert not res.success and "Access denied" in (res.error or "")

    def test_codegen_clamp_symlink_escape_denied(self, tmp_path):
        from integrations.adapters.codegen import CodegenAdapter

        adapter = CodegenAdapter(str(tmp_path))
        assert adapter._clamp(str(tmp_path / ".." / "escape.py")) is None
        outside = tempfile.mkdtemp(prefix="raivisox_outside13_")
        assert adapter._clamp(os.path.join(outside, "m.py")) is None


# ---------------------------------------------------------------------------
# Artifact registration keeps its independent realpath refusal
# ---------------------------------------------------------------------------


class TestArtifactRegistrationContainment:
    async def test_registration_rejects_outside_root_path(self):
        os.environ.setdefault(
            "DATABASE_URL",
            "sqlite+aiosqlite:///workspaces/tasks/phase0_containment/phase0.db",
        )
        from app import main as app_main

        outside = tempfile.mkdtemp(prefix="raivisox_phase0_out_")
        outside_file = os.path.join(outside, "artifact.bin")
        with open(outside_file, "wb") as f:
            f.write(b"data")
        with pytest.raises(ValueError, match="escaped the task workspace"):
            await app_main._register_task_artifact(
                "phase0-task",
                path=outside_file,
                node_id="step-1",
                step_db_id=None,
                step_title="t",
                run_id=None,
            )

    @needs_symlinks
    async def test_registration_rejects_symlink_resolved_outside(self):
        os.environ.setdefault(
            "DATABASE_URL",
            "sqlite+aiosqlite:///workspaces/tasks/phase0_containment/phase0.db",
        )
        from app import main as app_main

        outside = tempfile.mkdtemp(prefix="raivisox_phase0_out2_")
        outside_file = os.path.join(outside, "artifact.bin")
        with open(outside_file, "wb") as f:
            f.write(b"data")
        os.makedirs(app_main.TASK_WORKSPACE, exist_ok=True)
        link = os.path.join(app_main.TASK_WORKSPACE, "phase0_escape_link.bin")
        if os.path.lexists(link):
            os.unlink(link)
        try:
            os.symlink(outside_file, link)
            with pytest.raises(ValueError, match="escaped the task workspace"):
                await app_main._register_task_artifact(
                    "phase0-task",
                    path=link,
                    node_id="step-1",
                    step_db_id=None,
                    step_title="t",
                    run_id=None,
                )
        finally:
            if os.path.lexists(link):
                os.unlink(link)

    async def test_registration_accepts_legitimate_workspace_file(self):
        os.environ.setdefault(
            "DATABASE_URL",
            "sqlite+aiosqlite:///workspaces/tasks/phase0_containment/phase0.db",
        )
        from app import main as app_main
        from app import database as database_pkg

        # The registration path uses its own session over the shared engine:
        # the sqlite file's parent directory must exist before connecting,
        # and the (fresh) test database needs the registry schema.
        os.makedirs(
            os.path.join("workspaces", "tasks", "phase0_containment"),
            exist_ok=True,
        )
        async with database_pkg.engine.begin() as conn:
            await conn.run_sync(
                lambda sync: database_pkg.Base.metadata.create_all(sync)
            )
        target_dir = os.path.join(
            app_main.TASK_WORKSPACE, "phase0_legit"
        )
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "artifact.bin")
        with open(target, "wb") as f:
            f.write(b"legit")
        row = await app_main._register_task_artifact(
            "phase0-legit-task",
            path=target,
            node_id="step-1",
            step_db_id=None,
            step_title="t",
            run_id=None,
        )
        # Registration stores the raw-cased realpath; compare folded.
        assert os.path.normcase(row.storage_path) == realpath_norm(target)
        assert row.sha256
