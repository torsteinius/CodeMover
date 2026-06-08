"""Code Mover — Streamlit UI for safe git-based code transfer.

Workflow:
  Side A (sender)  — has git + LLM access. Generates a git format-patch
                     covering all commits since the last synced commit.
  Side B (receiver)— isolated environment. Receives the patch and applies
                     it with 'git am', preserving commit history.

Sync state (last synced commit hash) is stored in
  _code_mover_patches/sync.json
inside each repository.
"""

import re
import time
import streamlit as st
from pathlib import Path, PurePosixPath
from datetime import datetime

from core import (
    # Repo
    find_repo_root,
    validate_repo_markers,
    check_is_git_repo,
    compute_file_structure_snapshot,
    get_tracked_files,
    # Git state
    get_current_commit,
    get_commits_since,
    get_uncommitted_files,
    get_changed_files_since,
    # Sync state
    load_sync_state,
    save_sync_state,
    # Patch generation / application
    generate_format_patch,
    generate_format_patch_for_files,
    preview_format_patch,
    apply_format_patch,
    # File export
    export_selected_files,
    export_files_as_text,
    validate_full_load_project,
    preview_files_text,
    parse_and_apply_files_text,
    # LLM patch format
    parse_llm_patch,
    # ZIP
    export_patch_to_zip,
    import_patch_from_zip,
    # History
    add_to_history,
    get_patch_history_summary,
)




from config import (
    load_config,
    set_side,
    get_side,
    add_repo,
    remove_repo,
    set_active_repo,
    get_active_repo,
    discover_repos,
)


def _group_results_by_status(results: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for result in results:
        groups.setdefault(result.get("status", "unknown"), []).append(result)
    return groups


def _show_full_load_result_summary(results: list[dict]) -> dict[str, list[dict]]:
    groups = _group_results_by_status(results)
    written = groups.get("written", [])
    identical = groups.get("identical", [])
    trailing_only = groups.get("trailing_whitespace_only", [])
    errors = groups.get("error", [])

    skipped = len(identical) + len(trailing_only)
    if written:
        st.success(f"✅ {len(written)} fil(er) skrevet.")
    if skipped:
        st.info(f"✅ {skipped} fil(er) var allerede på plass og ble ikke skrevet.")
    if errors:
        st.error(f"❌ {len(errors)} feil:")
        for r in errors:
            st.write(f"• `{r['path']}`: {r.get('error', 'ukjent feil')}")

    return groups


def _show_full_load_preview(results: list[dict], repo_root: Path) -> dict[str, list[dict]]:
    groups = _group_results_by_status(results)
    new_files = groups.get("new", [])
    identical = groups.get("identical", [])
    trailing_only = groups.get("trailing_whitespace_only", [])
    changed = groups.get("changed", [])
    errors = groups.get("error", [])

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Filer", len(results))
    col2.metric("Nye", len(new_files))
    col3.metric("Identiske", len(identical))
    col4.metric("Kun slutt-space", len(trailing_only))
    col5.metric("Endres", len(changed))

    new_dirs = sorted({
        str(PurePosixPath(r["path"]).parent)
        for r in new_files
        if str(PurePosixPath(r["path"]).parent) != "."
        and not (repo_root / PurePosixPath(r["path"]).parent).exists()
    })

    if new_dirs:
        st.warning(
            "Disse mappene finnes ikke og vil bli opprettet: "
            + ", ".join(f"`{d}`" for d in new_dirs)
        )
    if new_files:
        with st.expander(f"ℹ️ {len(new_files)} ny(e) fil(er) vil bli opprettet"):
            for r in new_files:
                st.write(f"• `{r['path']}`")
    if changed:
        with st.expander(f"♻️ {len(changed)} eksisterende fil(er) vil bli skrevet"):
            for r in changed:
                st.write(f"• `{r['path']}`")
    if identical:
        with st.expander(f"✅ {len(identical)} identiske fil(er) hoppes over"):
            for r in identical:
                st.write(f"• `{r['path']}`")
    if trailing_only:
        with st.expander(
            f"✅ {len(trailing_only)} fil(er) avviker bare helt på slutten"
        ):
            for r in trailing_only:
                st.write(f"• `{r['path']}`")
    if errors:
        st.error(f"❌ {len(errors)} ugyldige fil(er):")
        for r in errors:
            st.write(f"• `{r['path']}`: {r.get('error', 'ukjent feil')}")

    return groups


def _show_full_load_project_guard(text: str, repo_root: Path, key: str) -> bool:
    validation = validate_full_load_project(text, repo_root)
    source = validation["source"]
    target = validation["target"]

    if validation["status"] == "match":
        st.success(
            f"🔒 Prosjekt-fingeravtrykk matcher `{target['repo_name']}`."
        )
        return True

    if validation["status"] == "weak_match":
        st.warning(
            f"Repo-/hovedmappenavn matcher `{target['repo_name']}`, "
            "men Code Mover fant ingen felles ankerfiler å kontrollere. "
            "Dette kan være riktig ved delvis full-load."
        )
        return st.checkbox(
            "Jeg har manuelt sjekket at dette er riktig prosjekt",
            key=f"{key}_weak_project_match",
        )

    if validation["status"] == "missing":
        st.warning(
            "⚠️ Full-load teksten mangler prosjekt-fingeravtrykk. "
            "Dette er trolig en eldre pakke, så Code Mover kan ikke bekrefte "
            "at dette er riktig prosjekt."
        )
        return st.checkbox(
            "Jeg har manuelt sjekket at dette er riktig prosjekt",
            key=f"{key}_missing_fingerprint",
        )

    st.error(
        "⛔ Prosjekt-fingeravtrykk matcher ikke. "
        "Dette ser ut som full-load fra et annet prosjekt."
    )
    st.write(
        f"Pakke: `{source.get('source_repo', '?')}` · "
        f"`{source.get('project_fingerprint', '?')[:12]}`"
    )
    st.write(
        f"Dette repoet: `{target['repo_name']}` · "
        f"`{target['fingerprint'][:12]}`"
    )
    return False


st.set_page_config(page_title="Code Mover", layout="wide")
st.title("📦 Code Mover")
st.caption("Git-based code transfer for isolated environments")


# ─── Cached file listing ─────────────────────────────────────────────────────
# get_tracked_files() does a git ls-files + OS walk — expensive on slow disks.
# Cache the result for 60 s so checkbox clicks don't retrigger a full scan.

@st.cache_data(ttl=60, show_spinner=False)
def _list_files(repo_root_str: str) -> list:
    return get_tracked_files(Path(repo_root_str))


# ─── Session state ──────────────────────────────────────────────────────

if "generated_patch" not in st.session_state:
    st.session_state["generated_patch"] = None      # raw patch text
if "generated_meta" not in st.session_state:
    st.session_state["generated_meta"] = {}         # metadata dict
if "discovered_repos" not in st.session_state:
    st.session_state["discovered_repos"] = []


# ─── Sidebar ────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Konfigurasjon")
    config = load_config()

    # ── Side selector ──
    current_side = config.get("side", "a")
    side_labels = {
        "a": "📤 **Side A — Avsender**\nHar git + LLM-tilgang. Genererer patcher.",
        "b": "📥 **Side B — Mottaker**\nIsolert miljø. Mottar og appliserer patcher.",
    }
    st.markdown("### 🔀 Hvilken side er dette?")

    new_side = st.radio(
        "Velg side",
        options=["a", "b"],
        format_func=lambda x: side_labels[x],
        index=0 if current_side == "a" else 1,
        key="side_selector",
    )

    if new_side != current_side:
        if current_side == "b" and new_side == "a":
            st.warning(
                "⚠️ Du bytter fra **mottaker (B)** til **avsender (A)**.\n\n"
                "Pass på at repoet er synkronisert med B før du genererer nye patcher."
            )
            col_yes, col_no = st.columns(2)
            with col_yes:
                if st.button("✅ Bekreft", use_container_width=True, key="sidebar_side_change_confirm"):
                    set_side(new_side)
                    st.rerun()
            with col_no:
                if st.button("❌ Avbryt", use_container_width=True, key="sidebar_side_change_cancel"):
                    st.rerun()
        else:
            set_side(new_side)
            st.rerun()

    st.divider()

    # ── Active repo ──
    st.subheader("📂 Aktivt repo")
    repo_names = [r["name"] for r in config.get("repos", [])]
    active_repo = get_active_repo()

    if repo_names:
        active_index = 0
        if active_repo:
            try:
                active_index = repo_names.index(active_repo["name"])
            except ValueError:
                active_index = 0

        selected = st.selectbox(
            "Velg repo", options=repo_names, index=active_index, key="repo_selector"
        )
        if selected != (active_repo["name"] if active_repo else None):
            set_active_repo(selected)
            st.rerun()
    else:
        st.info("Ingen repoer registrert.")

    active_repo = get_active_repo()
    if active_repo:
        repo_path = Path(active_repo["path"])
        st.success(f"✅ **{active_repo['name']}**")
        st.caption(f"`{repo_path}`")

        missing = validate_repo_markers(repo_path, active_repo.get("markers", ["app.py", "core.py"]))
        if missing:
            st.warning(f"⚠️ Mangler markører: {', '.join(missing)}")
        else:
            st.caption("✅ Alle markører funnet")

        if check_is_git_repo(repo_path):
            try:
                head = get_current_commit(repo_path)
                st.caption(f"🔖 HEAD: `{head[:12]}`")
            except Exception:
                pass

            sync = load_sync_state(repo_path)
            last_sync = sync.get("last_synced_commit")
            if last_sync:
                st.caption(f"🔗 Sist synket: `{last_sync[:12]}`")
                st.caption(f"📅 {sync.get('synced_at', '?')}")
            else:
                st.caption("🔗 Ikke synket ennå")
        else:
            st.warning("⚠️ Ikke et git-repo")

    st.divider()

    # ── Repo management ──
    with st.expander("➕ Legg til / fjern repo"):

        # Search button — results stored in session state so they survive reruns
        if st.button("🔍 Søk etter repoer", use_container_width=True, key="sidebar_repo_search"):
            with st.spinner("Søker..."):
                found = discover_repos()
            # Filter out already-registered repos
            existing_paths = {r["path"] for r in config.get("repos", [])}
            st.session_state["discovered_repos"] = [
                r for r in found if r["path"] not in existing_paths
            ]
            if not st.session_state["discovered_repos"]:
                st.info("Ingen nye repoer funnet.")

        # Show discovered repos persistently until all are added/dismissed
        discovered = st.session_state.get("discovered_repos", [])
        if discovered:
            st.caption(f"{len(discovered)} repo(er) funnet — klikk for å legge til:")
            for r in list(discovered):
                col_name, col_path, col_btn = st.columns([2, 4, 1])
                with col_name:
                    icon = "📁" if r.get("has_git") else "📂"
                    st.write(f"{icon} **{r['name']}**")
                with col_path:
                    st.caption(f"`{r['path']}`")
                with col_btn:
                    if st.button("＋", key=f"add_{r['path']}"):
                        add_repo(r["name"], r["path"])
                        set_active_repo(r["name"])
                        # Remove from discovered list
                        st.session_state["discovered_repos"] = [
                            x for x in st.session_state["discovered_repos"]
                            if x["path"] != r["path"]
                        ]
                        st.rerun()

        # Manual fallback: just a path — name is derived from folder name
        st.divider()
        st.caption("Legg til manuelt:")
        manual_path = st.text_input(
            "Sti til repo",
            placeholder="/Users/bruker/Documents/GitHub/mitt-repo",
            label_visibility="collapsed",
        )
        if st.button("➕ Legg til", use_container_width=True, disabled=not manual_path.strip(), key="sidebar_repo_add_manual"):
            p = Path(manual_path.strip())
            if p.is_dir():
                add_repo(p.name, str(p))
                set_active_repo(p.name)
                st.rerun()
            else:
                st.error("Finner ikke mappen.")

        # Remove repo
        if repo_names:
            st.divider()
            to_remove = st.selectbox("Fjern repo", options=[""] + repo_names, key="remove_selector")
            if to_remove and st.button("🗑️ Fjern", type="secondary", use_container_width=True, key="sidebar_repo_remove"):
                remove_repo(to_remove)
                st.rerun()


# ─── Guard: must have active repo ───────────────────────────────────────

active_repo = get_active_repo()
if not active_repo:
    st.warning("⚠️ Ingen repo valgt. Gå til sidemenyen for å legge til og velge et repo.")
    st.stop()

repo_root   = Path(active_repo["path"])
current_side = get_side()

if not check_is_git_repo(repo_root):
    st.error("❌ Det valgte repoet er ikke et git-repo. Code Mover krever git.")
    st.stop()


# ─── Tabs ───────────────────────────────────────────────────────────────

if current_side == "a":
    tab_generate, tab_files, tab_load, tab_history = st.tabs(
        ["📤 Generer patch", "📁 Enkeltfiler", "📦 Full load", "📜 Historikk"]
    )
else:
    tab_apply, tab_llm, tab_load, tab_history = st.tabs(
        ["📥 Apply patch", "🤖 LLM Patch", "📦 Full load", "📜 Historikk"]
    )




# ═══════════════════════════════════════════════════════════════════════
# SIDE A — Generate
# ═══════════════════════════════════════════════════════════════════════

if current_side == "a":
    with tab_generate:
        st.subheader("📤 Generer patch fra git-historikk")

        # ── Git status ──────────────────────────────────────────────────
        try:
            head_hash   = get_current_commit(repo_root)
            sync_state  = load_sync_state(repo_root)
            last_sync   = sync_state.get("last_synced_commit")
        except Exception as e:
            st.error(f"❌ Klarte ikke å lese git-status: {e}")
            st.stop()

        # Warn about uncommitted changes
        dirty = get_uncommitted_files(repo_root)
        if dirty:
            with st.expander(f"⚠️ {len(dirty)} ucommittede endringer", expanded=True):
                for f in dirty:
                    st.write(f"`{f['status']}` {f['path']}")
            st.warning(
                "Disse endringene er **ikke** med i patchen. "
                "Commit dem på Side A før du genererer."
            )

        # ── Show commits to be patched ──────────────────────────────────
        if last_sync:
            st.caption(f"Sist synket commit: `{last_sync[:12]}`")
            try:
                commits_to_patch = get_commits_since(repo_root, last_sync)
            except Exception as e:
                st.error(f"❌ git log feilet: {e}")
                st.stop()

            if not commits_to_patch:
                st.success("✅ Ingen nye commits siden sist sync — ingenting å patche.")
                st.stop()

            st.info(f"**{len(commits_to_patch)} commit(er)** vil bli inkludert i patchen:")
            for c in commits_to_patch:
                st.write(f"• `{c['hash']}` {c['message']}")
        else:
            st.warning(
                "⚠️ Ingen sync-historikk funnet. "
                "**Første patch** vil inkludere alle commits i repoet."
            )
            try:
                all_commits = get_commits_since(repo_root, "4b825dc642cb6eb9a060e54bf8d69288fbee4904")
            except Exception:
                all_commits = []
            st.caption(f"Totalt {len(all_commits)} commit(er) i repoet.")
            commits_to_patch = all_commits

        # ── Generate button ─────────────────────────────────────────────
        st.divider()
        description = st.text_input(
            "📝 Beskrivelse (valgfritt)",
            placeholder="F.eks. 'Sprint 12 — IMO-normalisering og fleetpanel-fix'",
        )

        if st.button("📤 Generer patch", type="primary", use_container_width=True, key="sender_generate_patch"):
            try:
                with st.spinner("Kjører git format-patch..."):
                    patch_text = generate_format_patch(repo_root, last_sync)

                meta = {
                    "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_side":    "a",
                    "since_commit":   last_sync or "(første sync)",
                    "head_commit":    head_hash,
                    "commit_count":   len(commits_to_patch),
                    "description":    description,
                }
                st.session_state["generated_patch"] = patch_text
                st.session_state["generated_meta"]  = meta

                add_to_history(
                    repo_root,
                    status="generated",
                    side="a",
                    since_hash=last_sync or "",
                    head_hash=head_hash,
                    commits=commits_to_patch,
                    description=description,
                )
                st.rerun()
            except Exception as e:
                st.error(f"❌ {e}")

        # ── Transfer options ─────────────────────────────────────────────
        if st.session_state.get("generated_patch"):
            patch_text = st.session_state["generated_patch"]
            meta       = st.session_state["generated_meta"]

            st.divider()
            st.subheader("✅ Patch klar — velg overføringsmetode")

            preview = preview_format_patch(patch_text)
            st.caption(
                f"{meta.get('commit_count', '?')} commit(er) · "
                f"{len(preview['files_changed'])} fil(er) endret"
            )

            # ── Confirm transfer (always visible, no scrolling needed) ──
            st.divider()
            st.subheader("✅ Bekreft overføring")
            st.caption(
                "Merk av når patchen er overført til Side B. "
                "Da lagres dette sync-punktet, og neste patch vil kun inneholde nye commits."
            )

            if st.button("✅ Bekreft at patchen er overført", type="primary", use_container_width=True, key="sender_confirm_patch_transferred"):
                save_sync_state(repo_root, head_hash)
                st.session_state["generated_patch"] = None
                st.session_state["generated_meta"] = {}
                st.success(f"✅ Sync-punkt lagret: `{head_hash[:12]}`")
                st.rerun()

            st.divider()

            tab_copy, tab_zip, tab_file = st.tabs(
                ["📋 Kopier tekst", "📦 Last ned ZIP", "💾 Last ned .patch-fil"]
            )

            with tab_copy:
                st.caption("Kopier patchen og lim inn på Side B.")
                st.markdown(
                    f'<div style="max-height:500px;overflow-y:auto;border:1px solid #ddd;border-radius:4px;">',
                    unsafe_allow_html=True,
                )
                st.code(patch_text, language="diff", line_numbers=False)
                st.markdown('</div>', unsafe_allow_html=True)


            with tab_zip:
                st.caption("ZIP med patch og metadata.")
                zip_bytes = export_patch_to_zip(patch_text, meta)
                repo_name = repo_root.name
                st.download_button(
                    "📦 Last ned .zip",
                    data=zip_bytes,
                    file_name=f"patch_{repo_name}_{meta.get('head_commit','')[:8]}.zip",
                    mime="application/zip",
                    use_container_width=True,
                )

            with tab_file:
                st.caption("Rå git patch-fil (.patch).")
                repo_name = repo_root.name
                st.download_button(
                    "💾 Last ned .patch",
                    data=patch_text,
                    file_name=f"patch_{repo_name}_{meta.get('head_commit','')[:8]}.patch",
                    mime="text/plain",
                    use_container_width=True,
                )

            with st.expander("🔍 Forhåndsvis endringer"):
                if preview["commits"]:
                    st.write("**Commits:**")
                    for c in preview["commits"]:
                        st.write(f"• `{c['hash']}` {c['subject']}")
                if preview["files_changed"]:
                    st.write("**Filer endret:**")
                    for f in preview["files_changed"]:
                        st.write(f"• `{f}`")
                with st.expander("Vis råpatch"):
                    st.code(patch_text, language="diff")



# ═══════════════════════════════════════════════════════════════════════
# SIDE A — Enkeltfiler
# ═══════════════════════════════════════════════════════════════════════

# ─── Shared file-tree picker (streamlit-tree-select) ────────────────────────


def _age_emoji(path: Path) -> str:
    """Return a coloured circle emoji indicating how recently the file was saved.

    🔴  ≤ 1 h      (very hot — just edited)
    🟠  1–6 h
    🟡  6–24 h
    ⚪  1–3 days
       (none)  > 3 days
    """
    try:
        age_s = time.time() - path.stat().st_mtime
    except OSError:
        return ""
    if age_s <= 3_600:    return "🔴 "
    if age_s <= 21_600:   return "🟠 "
    if age_s <= 86_400:   return "🟡 "
    if age_s <= 259_200:  return "⚪ "
    return ""


def _age_text(path: Path) -> str:
    """Return a human-readable relative age string like '2t 15m' or '3d 4t'."""
    try:
        age_s = int(time.time() - path.stat().st_mtime)
    except OSError:
        return ""
    if age_s < 60:
        return f"{age_s}s"
    if age_s < 3_600:
        return f"{age_s // 60}m"
    if age_s < 86_400:
        return f"{age_s // 3_600}t {(age_s % 3_600) // 60}m"
    if age_s < 604_800:
        return f"{age_s // 86_400}d {(age_s % 86_400) // 3_600}t"
    return f"{age_s // 86_400}d"


def _build_tree_nodes(files: list, repo_root: Path | None = None) -> list:
    """Convert a flat sorted file list into nested nodes for tree_select.

    Folders become parent nodes (value prefixed with ``__dir__``).
    Files become leaf nodes whose value is the relative path string.
    """
    def _insert(node: dict, parts: tuple, full_path: str) -> None:
        if len(parts) == 1:
            node["files"].append(full_path)
        else:
            node["dirs"].setdefault(parts[0], {"files": [], "dirs": {}})
            _insert(node["dirs"][parts[0]], parts[1:], full_path)

    root: dict = {"files": [], "dirs": {}}
    for f in sorted(files):
        _insert(root, PurePosixPath(f).parts, f)

    def _to_nodes(node: dict, dir_path: str = "") -> list:
        result = []
        for dirname in sorted(node["dirs"]):
            child_path = f"{dir_path}/{dirname}" if dir_path else dirname
            result.append({
                "label": f"📁 {dirname}",
                "value": f"__dir__{child_path}",
                "children": _to_nodes(node["dirs"][dirname], child_path),
            })
        for f in node["files"]:
            emoji = _age_emoji(repo_root / f) if repo_root else ""
            age_label = _age_text(repo_root / f) if repo_root else ""
            label = f"{emoji}{PurePosixPath(f).name}"
            if age_label:
                label += f"  ─ {age_label}"
            result.append({
                "label": label,
                "value": f,
            })
        return result

    return _to_nodes(root)


def _render_file_tree(files: list, key_prefix: str, repo_root: Path | None = None) -> list:
    """Render a recursive checkbox tree and return selected file paths.

    Uses streamlit-tree-select so folders can be expanded/collapsed and an
    entire branch can be selected with a single click.
    File age is shown as a coloured emoji next to each filename.

    Args:
        files:      Flat sorted list of relative file paths.
        key_prefix: ``"ef"`` for Enkeltfiler, ``"fl"`` for Full Load.
                    Keeps the two trees' selections independent.

    Returns:
        List of selected file paths (leaf values only, no ``__dir__`` entries).
    """
    from streamlit_tree_select import tree_select

    if not files:
        st.info("Ingen filer funnet.")
        return []

    nodes = _build_tree_nodes(files, repo_root)

    # Restore previous checkbox + expand state so selections survive reruns
    prev_checked  = st.session_state.get(f"{key_prefix}_checked", [])
    prev_expanded = st.session_state.get(f"{key_prefix}_expanded", [])

    result = tree_select(
        nodes,
        check_model="all",        # returns both parent+leaf; we filter __dir__ ourselves
        checked=prev_checked,
        expanded=prev_expanded,
        show_expand_all=True,
        key=f"{key_prefix}_tree",
    )

    # Streamlit also stores component value in session_state[key]; prefer that
    component_state = st.session_state.get(f"{key_prefix}_tree") or result or {}
    checked  = component_state.get("checked",  prev_checked)
    expanded = component_state.get("expanded", prev_expanded)
    st.session_state[f"{key_prefix}_checked"]  = checked
    st.session_state[f"{key_prefix}_expanded"] = expanded

    # Strip __dir__ sentinel values — keep only actual file paths
    return [v for v in checked if not v.startswith("__dir__")]


def _filter_files_by_age(files: list[str], repo_root: Path, age_seconds: float) -> list[str]:
    """Return only files modified within the last ``age_seconds`` seconds."""
    now = time.time()
    result = []
    for f in files:
        full_path = repo_root / f
        try:
            if full_path.exists() and (now - full_path.stat().st_mtime) <= age_seconds:
                result.append(f)
        except OSError:
            pass
    return result


if current_side == "a":
    with tab_files:
        st.subheader("📁 Velg enkeltfiler å overføre")
        st.caption(
            "Velg spesifikke filer endret siden sist sync. "
            "Genererer en **git format-patch** kun for valgte filer."
        )

        # ── Hent git-status ─────────────────────────────────────────────
        try:
            head_hash  = get_current_commit(repo_root)
            sync_state = load_sync_state(repo_root)
            last_sync  = sync_state.get("last_synced_commit")
        except Exception as e:
            st.error(f"❌ {e}")
            st.stop()

        try:
            changed_files = get_changed_files_since(repo_root, last_sync or "")
        except Exception:
            changed_files = []

        if not changed_files:
            st.info("Ingen endrede filer siden sist sync.")
            st.stop()

        # ── Tidsfilter ──────────────────────────────────────────────────
        st.caption("⏱️ Filtrer på sist endret:")
        age_cols = st.columns([1, 1, 1, 4])
        age_filter = None
        with age_cols[0]:
            if st.button("🕐 Siste time", use_container_width=True, key="ef_age_1h"):
                age_filter = 3_600
        with age_cols[1]:
            if st.button("📅 Siste døgn", use_container_width=True, key="ef_age_24h"):
                age_filter = 86_400
        with age_cols[2]:
            if st.button("📆 Siste uke", use_container_width=True, key="ef_age_7d"):
                age_filter = 604_800

        display_files = changed_files
        if age_filter is not None:
            filtered = _filter_files_by_age(changed_files, repo_root, age_filter)
            if len(filtered) < len(changed_files):
                st.info(f"Viser {len(filtered)} av {len(changed_files)} fil(er) — {len(changed_files) - len(filtered)} eldre fil(er) skjult")
            # Auto-check all filtered files
            st.session_state["ef_checked"] = list(filtered)
            display_files = filtered

        # ── Trevisning med checkboxer ────────────────────────────────────
        selected_files = _render_file_tree(display_files, "ef", repo_root)

        st.divider()
        st.caption(f"**{len(selected_files)}** av {len(changed_files)} fil(er) valgt")

        if selected_files:
            description = st.text_input(
                "📝 Beskrivelse (valgfritt)",
                key="files_desc",
                placeholder="F.eks. 'Fikset IMO-oppslag i vessel_tracker.py'",
            )

            if st.button("📤 Generer patch", type="primary", use_container_width=True, key="ef_generate"):
                try:
                    with st.spinner("Kjører git format-patch..."):
                        patch_text = generate_format_patch_for_files(
                            repo_root, last_sync, selected_files
                        )
                    meta = {
                        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "source_side":    "a",
                        "since_commit":   last_sync or "(første sync)",
                        "head_commit":    head_hash,
                        "selected_files": selected_files,
                        "partial":        True,
                    }
                    st.session_state["generated_patch"] = patch_text
                    st.session_state["generated_meta"]  = meta
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ {e}")

        # ── Vis generert patch ───────────────────────────────────────────
        if st.session_state.get("generated_patch") and st.session_state["generated_meta"].get("partial"):
            patch_text = st.session_state["generated_patch"]
            meta       = st.session_state["generated_meta"]

            st.divider()
            st.subheader("✅ Patch klar")

            tab_copy, tab_zip, tab_file = st.tabs(
                ["📋 Kopier tekst", "📦 Last ned ZIP", "💾 Last ned .patch-fil"]
            )
            with tab_copy:
                st.text_area("patch", value=patch_text, height=400, label_visibility="collapsed")
            with tab_zip:
                st.download_button(
                    "📦 Last ned .zip",
                    data=export_patch_to_zip(patch_text, meta),
                    file_name=f"patch_{repo_root.name}_{head_hash[:8]}_partial.zip",
                    mime="application/zip",
                    use_container_width=True,
                )
            with tab_file:
                st.download_button(
                    "💾 Last ned .patch",
                    data=patch_text,
                    file_name=f"patch_{repo_root.name}_{head_hash[:8]}_partial.patch",
                    mime="text/plain",
                    use_container_width=True,
                )



# ═══════════════════════════════════════════════════════════════════════
# SIDE B — Apply
# ═══════════════════════════════════════════════════════════════════════


if current_side == "b":
    with tab_apply:
        st.subheader("📥 Apply patch fra Side A")

        sync_state = load_sync_state(repo_root)
        last_sync  = sync_state.get("last_synced_commit")
        if last_sync:
            st.caption(f"Sist synket: `{last_sync[:12]}` ({sync_state.get('synced_at', '?')})")
        else:
            st.caption("Ikke synket ennå — første patch vil etablere sync-baseline.")

        dirty = get_uncommitted_files(repo_root)
        if dirty:
            st.warning(
                f"⚠️ Repoet har {len(dirty)} ucommittede endringer. "
                "git am kan feile hvis disse er i konflikt med patchen."
            )

        st.divider()

        # ── Input method ────────────────────────────────────────────────
        input_method = st.radio(
            "Inndatametode",
            options=["📋 Lim inn tekst", "📦 Last opp ZIP", "💾 Last opp .patch-fil"],
            horizontal=True,
        )

        patch_text = ""
        patch_meta = {}

        if input_method == "📋 Lim inn tekst":
            patch_text = st.text_area(
                "patch",
                height=300,
                placeholder="Lim inn patch-tekst fra Side A her...",
                label_visibility="collapsed",
            )

        elif input_method == "📦 Last opp ZIP":
            uploaded = st.file_uploader("Last opp .zip", type=["zip"])
            if uploaded:
                try:
                    patch_text, patch_meta = import_patch_from_zip(uploaded.getvalue())
                    st.success(f"✅ ZIP lastet inn")
                    with st.expander("📄 Metadata"):
                        st.json(patch_meta)
                except Exception as e:
                    st.error(f"❌ {e}")

        elif input_method == "💾 Last opp .patch-fil":
            uploaded = st.file_uploader("Last opp .patch-fil", type=["patch", "txt"])
            if uploaded:
                try:
                    patch_text = uploaded.getvalue().decode("utf-8")
                    st.success(f"✅ Patch-fil lastet inn ({len(patch_text)} tegn)")
                except Exception as e:
                    st.error(f"❌ Kunne ikke lese fil: {e}")

        # ── Auto-detect format and apply ────────────────────────────────
        if patch_text.strip():
            is_full_load = (
                "----==============" in patch_text
                and not patch_text.strip().startswith("From ")
            )

            is_llm_patch = (
                re.search(
                    r"^----======\s+"
                    r"(FILE|OVERWRITE|DELETE|PATCH|INSERT_AFTER|INSERT_BEFORE|DELETE_BLOCK)"
                    r":\s+.+$",
                    patch_text,
                    flags=re.MULTILINE,
                )
                is not None
                and not patch_text.strip().startswith("From ")
            )

            # ── LLM PATCH FORMAT ─────────────────────────────────────────
            if is_llm_patch:
                st.info(
                    "🤖 **LLM Patch-format detektert** — dette er ikke en git patch. "
                    "Patchen vil bli applisert med Code Movers interne LLM-patchmotor, "
                    "ikke med `git am`."
                )

                st.warning(
                    "⚠️ LLM Patch gjør direkte filendringer på disk. "
                    "Sørg for at repoet er i git, slik at du kan se diff og rulle tilbake."
                )

                if st.button(
                    "🤖 Apply LLM patch",
                    type="primary",
                    use_container_width=True,
                    key="apply_tab_llm_patch",
                ):
                    try:
                        with st.spinner("Appliserer LLM patch..."):
                            results = parse_llm_patch(patch_text, repo_root)

                        created = [r for r in results if r["status"] == "created"]
                        overwritten = [r for r in results if r["status"] == "overwritten"]
                        patched = [r for r in results if r["status"] == "patched"]
                        inserted_after = [r for r in results if r["status"] == "inserted_after"]
                        inserted_before = [r for r in results if r["status"] == "inserted_before"]
                        deleted_block = [r for r in results if r["status"] == "deleted_block"]
                        deleted = [r for r in results if r["status"] == "deleted"]
                        written = [r for r in results if r["status"] == "written"]
                        errors = [r for r in results if r["status"] == "error"]

                        changed_count = (
                            len(created)
                            + len(overwritten)
                            + len(patched)
                            + len(inserted_after)
                            + len(inserted_before)
                            + len(deleted_block)
                            + len(deleted)
                            + len(written)
                        )

                        if changed_count:
                            st.success(
                                f"✅ {changed_count} operasjon(er) utført i "
                                f"`{repo_root.name}`"
                            )

                        if created:
                            st.write(f"🆕 {len(created)} fil(er) opprettet")
                        if overwritten:
                            st.write(f"♻️ {len(overwritten)} fil(er) overskrevet")
                        if patched:
                            st.write(f"🔧 {len(patched)} blokk(er) erstattet")
                        if inserted_after:
                            st.write(f"➕ {len(inserted_after)} insert-after utført")
                        if inserted_before:
                            st.write(f"➕ {len(inserted_before)} insert-before utført")
                        if deleted_block:
                            st.write(f"✂️ {len(deleted_block)} blokk(er) slettet")
                        if deleted:
                            st.write(f"🗑️ {len(deleted)} fil(er) slettet")
                        if written:
                            st.write(f"📝 {len(written)} fil(er) skrevet")

                        if errors:
                            st.error(f"❌ {len(errors)} feil:")
                            for r in errors:
                                st.write(
                                    f"• `{r['path']}`: {r.get('error', 'ukjent feil')}"
                                )

                        if changed_count and not errors:
                            st.balloons()

                    except Exception as e:
                        st.error(f"❌ {e}")

            # ── FULL LOAD FORMAT ─────────────────────────────────────────
            elif is_full_load:
                st.info(                    "📦 **Full load-format detektert** — dette er en tekstblokk med råfiler, "
                    "ikke en git patch. Filene vil bli skrevet direkte til disk."
                )

                # Parse file list (without writing yet)
                _blocks = re.split(
                    r"^----==============\s+(.+)$", patch_text, flags=re.MULTILINE
                )
                fl_files = []
                _i = 1
                while _i < len(_blocks) - 1:
                    fp = _blocks[_i].strip()
                    # Strip leading "PATH: ..." line if present (from export format)
                    content_lines = _blocks[_i + 1].lstrip("\n")
                    if fp:
                        fl_files.append(fp)
                    _i += 2
                preview_results = preview_files_text(patch_text, repo_root)

                if not preview_results:
                    st.warning("⚠️ Ingen filer funnet i teksten.")
                else:
                    existing_files = [f for f in fl_files if (repo_root / f).exists()]
                    new_files      = [f for f in fl_files if not (repo_root / f).exists()]
                    new_dirs       = sorted({
                        str(PurePosixPath(f).parent)
                        for f in new_files
                        if str(PurePosixPath(f).parent) != "."
                        and not (repo_root / PurePosixPath(f).parent).exists()
                    })

                    col1, col2, col3 = st.columns(3)
                    col1.metric("Filer i blokken", len(fl_files))
                    col2.metric("Finnes allerede", len(existing_files))
                    col3.metric("Nye filer", len(new_files))
                    preview_groups = _show_full_load_preview(preview_results, repo_root)
                    project_ok = _show_full_load_project_guard(
                        patch_text,
                        repo_root,
                        "patch_tab_full_load",
                    )
                    existing_count = (
                        len(preview_groups.get("identical", []))
                        + len(preview_groups.get("trailing_whitespace_only", []))
                        + len(preview_groups.get("changed", []))
                    )

                    ok_to_proceed = project_ok
                    pct_match = existing_count / len(preview_results)

                    # Warn if barely anything matches — likely wrong repo / path
                    if len(preview_results) > 3 and pct_match < 0.30:
                        st.error(
                            f"⚠️ Kun **{existing_count} av {len(preview_results)}** filer "
                            f"finnes i `{repo_root.name}`. "
                            "Dette kan bety at du er i feil repo eller feil mappe."
                        )
                        ok_to_proceed = st.checkbox(
                            "Jeg er sikker på at dette er rett sted — skriv filene likevel",
                            key="fl_confirm_wrong_repo",
                        )
                    else:
                        if new_dirs:
                            st.warning(
                                "📁 Disse mappene finnes ikke og vil bli opprettet: "
                                + ", ".join(f"`{d}`" for d in new_dirs)
                            )
                        if new_files:
                            with st.expander(
                                f"ℹ️ {len(new_files)} ny(e) fil(er) vil bli opprettet"
                            ):
                                for f in new_files:
                                    st.write(f"• `{f}`")
                        if existing_files:
                            with st.expander(
                                f"♻️ {len(existing_files)} eksisterende fil(er) finnes fra før"
                            ):
                                for f in existing_files:
                                    st.write(f"• `{f}`")

                    st.divider()
                    if ok_to_proceed:
                        if st.button(
                            "📦 Skriv filer til disk",
                            type="primary",
                            use_container_width=True,
                            key="receiver_apply_full_load_text",
                        ):
                            try:
                                with st.spinner("Skriver filer..."):
                                    results = parse_and_apply_files_text(
                                        patch_text, repo_root
                                    )
                                result_groups = _show_full_load_result_summary(results)
                                written = result_groups.get("written", [])
                                errors = result_groups.get("error", [])
                                if written:
                                    st.success(
                                        f"✅ {len(written)} fil(er) skrevet til "
                                        f"`{repo_root.name}`"
                                    )
                                if errors:
                                    st.error(f"❌ {len(errors)} feil:")
                                    for r in errors:
                                        st.write(
                                            f"• `{r['path']}`: {r.get('error', 'ukjent feil')}"
                                        )
                                if written and not errors:
                                    st.balloons()
                            except Exception as e:
                                st.error(f"❌ {e}")

            # ── GIT FORMAT-PATCH ─────────────────────────────────────────
            else:
                preview = preview_format_patch(patch_text)

                with st.expander("🔍 Forhåndsvis patch", expanded=True):
                    if preview["commits"]:
                        st.write("**Commits som vil bli applisert:**")
                        for c in preview["commits"]:
                            st.write(f"• `{c['hash']}` {c['subject']}")
                    if preview["files_changed"]:
                        st.write(f"**{len(preview['files_changed'])} fil(er) endres:**")
                        for f in preview["files_changed"]:
                            st.write(f"• `{f}`")

                st.divider()

                if st.button("🚀 Apply patch", type="primary", use_container_width=True, key="receiver_apply_format_patch"):
                    try:
                        with st.spinner("Kjører git am..."):
                            output = apply_format_patch(repo_root, patch_text)

                        new_head = get_current_commit(repo_root)
                        save_sync_state(repo_root, new_head)

                        add_to_history(
                            repo_root,
                            status="applied",
                            side="b",
                            since_hash=last_sync or "",
                            head_hash=new_head,
                            commits=preview["commits"],
                            description=patch_meta.get("description", ""),
                        )

                        st.success("✅ Patch applisert!")
                        st.caption(f"Ny HEAD: `{new_head[:12]}`")
                        if output:
                            st.code(output)
                        st.balloons()

                    except Exception as e:
                        st.error(f"❌ Apply feilet:\n\n{e}")


# ═══════════════════════════════════════════════════════════════════════
# SIDE B — LLM Patch (motta patch fra LLM, ingen git)
# ═══════════════════════════════════════════════════════════════════════


if current_side == "b":
    with tab_llm:
        st.subheader("🤖 LLM Patch — motta patch fra ChatGPT / annen LLM")
        st.caption(
            "Lim inn en patch generert av en LLM i **LLM Patch Format**. "
            "For eksisterende filer brukes små blokkendringer med `PATCH`, "
            "`INSERT_AFTER`, `INSERT_BEFORE` eller `DELETE_BLOCK`. "
            "`FILE` brukes kun for nye filer, og `OVERWRITE` bare ved eksplisitt "
            "full erstatning av en eksisterende fil."
        )

        with st.expander("📖 Hvordan få en LLM til å lage denne patchen?"):
            llm_instruction = "\n".join([
                "Bruk LLM Patch Format for Code Mover.",
                "",
                "Viktig:",
                "- Ikke send hele eksisterende filer.",
                "- For eksisterende filer skal du sende små blokkendringer.",
                "- Bruk FILE kun for nye filer.",
                "- Bruk OVERWRITE bare hvis hele eksisterende fil eksplisitt skal erstattes.",
                "- FIND-blokker må være eksakte og finnes nøyaktig én gang i filen.",
                "- Ikke bruk \"... resten uendret\" eller andre forkortelser.",
                "",
                "Støttet format:",
                "",
                "1) Erstatt en eksisterende kodeblokk:",
                "----====== PATCH: path/to/file.py",
                "@@",
                "FIND:",
                "<eksakt eksisterende tekst>",
                "",
                "REPLACE:",
                "<ny tekst>",
                "",
                "2) Sett inn etter en eksisterende blokk:",
                "----====== INSERT_AFTER: path/to/file.py",
                "@@",
                "FIND:",
                "<eksakt eksisterende tekst>",
                "",
                "INSERT:",
                "<tekst som skal settes inn etter FIND-blokken>",
                "",
                "3) Sett inn før en eksisterende blokk:",
                "----====== INSERT_BEFORE: path/to/file.py",
                "@@",
                "FIND:",
                "<eksakt eksisterende tekst>",
                "",
                "INSERT:",
                "<tekst som skal settes inn før FIND-blokken>",
                "",
                "4) Slett en eksisterende blokk:",
                "----====== DELETE_BLOCK: path/to/file.py",
                "@@",
                "FIND:",
                "<eksakt eksisterende tekst som skal fjernes>",
                "",
                "5) Opprett ny fil:",
                "----====== FILE: path/to/new_file.py",
                "<hele innholdet i ny fil>",
                "",
                "6) Erstatt hel eksisterende fil, kun hvis eksplisitt nødvendig:",
                "----====== OVERWRITE: path/to/existing_file.py",
                "<hele filinnholdet>",
                "",
                "7) Slett fil:",
                "----====== DELETE: path/to/file.py",
                "",
                "Når du lager patch:",
                "- Bruk relative filstier.",
                "- Del opp endringer i flere små operasjoner hvis det er tryggere.",
                "- Velg FIND-blokker som er lange nok til å være unike.",
                "- Hvis du ikke har nok kontekst til å lage en eksakt FIND-blokk, be om relevant utdrag av filen først.",
            ])

            st.markdown("Gi LLM-en denne instruksen:")
            st.code(llm_instruction, language="text")
            st.caption(
                "Tips: Last også opp `LLM_PATCH_FORMAT.md` til LLM-en hvis du vil gi full kontekst."
            )

        st.divider()

        llm_patch_text = st.text_area(
            "Lim inn LLM-patch her",
            height=400,
            placeholder=(
                "----====== PATCH: app.py\n"
                "@@\n"
                "FIND:\n"
                "gammel kode\n\n"
                "REPLACE:\n"
                "ny kode\n"
            ),
            key="llm_patch_input",
        )

        if llm_patch_text.strip():
            action_pattern = (
                r"^----======\s+"
                r"(FILE|OVERWRITE|DELETE|PATCH|INSERT_AFTER|INSERT_BEFORE|DELETE_BLOCK)"
                r":\s+(.+)$"
            )

            _blocks = re.split(action_pattern, llm_patch_text, flags=re.MULTILINE)

            operations = []
            _i = 1
            while _i < len(_blocks) - 2:
                action = _blocks[_i].strip().upper()
                fp = _blocks[_i + 1].strip()
                body = _blocks[_i + 2]
                if fp:
                    operations.append({"action": action, "path": fp, "body": body})
                _i += 3

            if not operations:
                st.warning(
                    "⚠️ Fant ingen gyldige LLM Patch-operasjoner. "
                    "Forventet `FILE`, `OVERWRITE`, `DELETE`, `PATCH`, "
                    "`INSERT_AFTER`, `INSERT_BEFORE` eller `DELETE_BLOCK`."
                )
            else:
                create_files = [op["path"] for op in operations if op["action"] == "FILE"]
                overwrite_files = [op["path"] for op in operations if op["action"] == "OVERWRITE"]
                delete_files = [op["path"] for op in operations if op["action"] == "DELETE"]
                patch_files = [
                    op["path"]
                    for op in operations
                    if op["action"] in {"PATCH", "INSERT_AFTER", "INSERT_BEFORE", "DELETE_BLOCK"}
                ]

                existing_create_files = [f for f in create_files if (repo_root / f).exists()]
                missing_patch_files = [f for f in patch_files if not (repo_root / f).exists()]
                missing_delete_files = [f for f in delete_files if not (repo_root / f).exists()]

                all_written_like = create_files + overwrite_files + patch_files
                new_dirs = sorted({
                    str(PurePosixPath(f).parent)
                    for f in all_written_like
                    if str(PurePosixPath(f).parent) != "."
                    and not (repo_root / PurePosixPath(f).parent).exists()
                })

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("🔧 Blokk-endringer", len(patch_files))
                col2.metric("🆕 Nye filer", len(create_files))
                col3.metric("♻️ Overwrite", len(overwrite_files))
                col4.metric("🗑️ Slett", len(delete_files))

                with st.expander("🔍 Operasjoner i patchen", expanded=True):
                    for op in operations:
                        exists = (repo_root / op["path"]).exists()
                        exists_label = "finnes" if exists else "ny/mangler"
                        st.write(f"• `{op['action']}` `{op['path']}` — {exists_label}")

                ok_to_proceed = True

                if existing_create_files:
                    st.error(
                        "`FILE` kan bare brukes for nye filer. Disse filene finnes allerede:"
                    )
                    for f in existing_create_files:
                        st.write(f"• `{f}`")
                    ok_to_proceed = False

                if missing_patch_files:
                    st.error("Disse blokk-endringene peker på filer som ikke finnes:")
                    for f in missing_patch_files:
                        st.write(f"• `{f}`")
                    ok_to_proceed = False

                if missing_delete_files:
                    st.warning("Disse filene er markert for sletting, men finnes ikke:")
                    for f in missing_delete_files:
                        st.write(f"• `{f}`")

                if new_dirs:
                    st.warning(
                        "Disse mappene finnes ikke og vil bli opprettet: "
                        + ", ".join(f"`{d}`" for d in new_dirs)
                    )

                if overwrite_files:
                    ok_to_proceed = st.checkbox(
                        "Jeg forstår at `OVERWRITE` erstatter hele filer",
                        key="llm_confirm_overwrite",
                    ) and ok_to_proceed

                st.divider()

                if ok_to_proceed:
                    if st.button(
                        "🤖 Apply LLM patch",
                        type="primary",
                        use_container_width=True,
                        key="llm_apply_patch_modern",
                    ):
                        try:
                            with st.spinner("Appliserer LLM patch..."):
                                results = parse_llm_patch(llm_patch_text, repo_root)

                            created = [r for r in results if r["status"] == "created"]
                            overwritten = [r for r in results if r["status"] == "overwritten"]
                            patched = [r for r in results if r["status"] == "patched"]
                            inserted_after = [r for r in results if r["status"] == "inserted_after"]
                            inserted_before = [r for r in results if r["status"] == "inserted_before"]
                            deleted_block = [r for r in results if r["status"] == "deleted_block"]
                            deleted = [r for r in results if r["status"] == "deleted"]
                            written = [r for r in results if r["status"] == "written"]
                            errors = [r for r in results if r["status"] == "error"]

                            changed_count = (
                                len(created)
                                + len(overwritten)
                                + len(patched)
                                + len(inserted_after)
                                + len(inserted_before)
                                + len(deleted_block)
                                + len(deleted)
                                + len(written)
                            )

                            if changed_count:
                                st.success(
                                    f"✅ {changed_count} operasjon(er) utført i "
                                    f"`{repo_root.name}`"
                                )

                            if created:
                                st.write(f"🆕 {len(created)} fil(er) opprettet")
                            if overwritten:
                                st.write(f"♻️ {len(overwritten)} fil(er) overskrevet")
                            if patched:
                                st.write(f"🔧 {len(patched)} blokk(er) erstattet")
                            if inserted_after:
                                st.write(f"➕ {len(inserted_after)} insert-after utført")
                            if inserted_before:
                                st.write(f"➕ {len(inserted_before)} insert-before utført")
                            if deleted_block:
                                st.write(f"✂️ {len(deleted_block)} blokk(er) slettet")
                            if deleted:
                                st.write(f"🗑️ {len(deleted)} fil(er) slettet")
                            if written:
                                st.write(f"📝 {len(written)} fil(er) skrevet")

                            if errors:
                                st.error(f"❌ {len(errors)} feil:")
                                for r in errors:
                                    st.write(
                                        f"• `{r['path']}`: {r.get('error', 'ukjent feil')}"
                                    )

                            if changed_count and not errors:
                                st.balloons()

                        except Exception as e:
                            st.error(f"❌ {e}")


# ═══════════════════════════════════════════════════════════════════════
# SIDE A — Full load (kopier hele filer som tekst)
# ═══════════════════════════════════════════════════════════════════════

if current_side == "a":

    with tab_load:
        st.subheader("📦 Full load — kopier hele filer som tekst")
        st.caption(
            "Velg filer og generer en enkel tekstblokk med `----============== <filnavn>`-separator. "
            "Ingen git, ingen patcher — bare råfilene satt sammen. "
            "Lim inn på Side B for å overskrive filene."
        )

        try:
            all_files = _list_files(str(repo_root))
        except Exception as e:
            st.error(f"❌ Kunne ikke liste filer: {e}")
            st.stop()

        if not all_files:
            st.info("Ingen tracked text-filer funnet.")
            st.stop()

        # ── Tidsfilter ──────────────────────────────────────────────────
        st.caption("⏱️ Filtrer på sist endret:")
        age_cols = st.columns([1, 1, 1, 4])
        age_filter = None
        with age_cols[0]:
            if st.button("🕐 Siste time", use_container_width=True, key="fl_age_1h"):
                age_filter = 3_600
        with age_cols[1]:
            if st.button("📅 Siste døgn", use_container_width=True, key="fl_age_24h"):
                age_filter = 86_400
        with age_cols[2]:
            if st.button("📆 Siste uke", use_container_width=True, key="fl_age_7d"):
                age_filter = 604_800

        # ── Filtrering ──────────────────────────────────────────────────
        filter_text = st.text_input("🔍 Filtrer filer", placeholder="f.eks. .py eller core/")
        if filter_text:
            filtered = [f for f in all_files if filter_text.lower() in f.lower()]
        else:
            filtered = all_files

        if age_filter is not None:
            age_filtered = _filter_files_by_age(filtered, repo_root, age_filter)
            if len(age_filtered) < len(filtered):
                st.info(f"Viser {len(age_filtered)} av {len(filtered)} fil(er) — {len(filtered) - len(age_filtered)} eldre fil(er) skjult")
            # Auto-check all filtered files
            st.session_state["fl_checked"] = list(age_filtered)
            filtered = age_filtered

        st.caption(f"Viser {len(filtered)} av {len(all_files)} fil(er)")

        # ── Trevisning med checkboxer ────────────────────────────────────
        selected_files = _render_file_tree(filtered, "fl", repo_root)

        # ── Generer tekst ───────────────────────────────────────────────
        st.divider()
        if selected_files:
            st.success(f"✅ {len(selected_files)} fil(er) valgt")

            if st.button("📦 Generer full load-tekst", type="primary", use_container_width=True, key="sender_generate_full_load_text"):
                try:
                    with st.spinner("Leser filer..."):
                        full_text = export_files_as_text(repo_root, selected_files)
                    st.session_state["full_load_text"] = full_text
                    st.session_state["full_load_file_count"] = len(selected_files)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ {e}")

        if st.session_state.get("full_load_text"):
            full_text       = st.session_state["full_load_text"]
            fl_file_count   = st.session_state.get("full_load_file_count", "?")

            st.divider()
            st.subheader("✅ Full load-tekst klar")

            col_info, col_dl, col_clear = st.columns([5, 2, 1])
            with col_info:
                st.caption(
                    f"{fl_file_count} fil(er) · {len(full_text):,} tegn — "
                    "marker alt i tekstboksen og kopier (Ctrl+A / ⌘+A)"
                )
            with col_dl:
                st.download_button(
                    "💾 Last ned .txt",
                    data=full_text,
                    file_name=f"full_load_{repo_root.name}.txt",
                    mime="text/plain",
                    use_container_width=True,
                )
            with col_clear:
                if st.button("🗑️", use_container_width=True, help="Fjern teksten", key="sender_clear_full_load_text"):
                    st.session_state["full_load_text"] = None
                    st.session_state["full_load_file_count"] = 0
                    st.rerun()

            st.text_area(
                "full_load_display",
                value=full_text,
                height=420,
                label_visibility="collapsed",
            )
        else:
            st.info("💡 Velg filer over og trykk 'Generer full load-tekst'")


# ═══════════════════════════════════════════════════════════════════════
# SIDE B — Full load (lim inn tekst og skriv filer)
# ═══════════════════════════════════════════════════════════════════════

if current_side == "b":
    with tab_load:
        st.subheader("📦 Full load — lim inn filer og overskriv")
        st.caption(
            "Lim inn tekstblokken fra Side A. "
            "Filene skrives til disk og **overskriver** eksisterende filer."
        )

        full_text = st.text_area(
            "Lim inn full load-tekst her",
            height=400,
            placeholder="----============== core/app.py\n...",
        )

        if full_text.strip():
            st.divider()
            preview_results = preview_files_text(full_text, repo_root)
            if preview_results:
                _show_full_load_preview(preview_results, repo_root)
            project_ok = _show_full_load_project_guard(
                full_text,
                repo_root,
                "full_load_tab",
            )
            st.warning(
                "⚠️ Dette vil **overskrive** filer på disk. "
                "Sørg for at du har backup eller at filene er i git."
            )

            if project_ok and st.button("📦 Skriv filer til disk", type="primary", use_container_width=True, key="receiver_full_load_tab_write_files"):
                try:
                    with st.spinner("Skriver filer..."):
                        results = parse_and_apply_files_text(full_text, repo_root)

                    result_groups = _show_full_load_result_summary(results)
                    written = result_groups.get("written", [])
                    errors = result_groups.get("error", [])

                    if written:
                        st.success(f"✅ {len(written)} fil(er) skrevet til `{repo_root}`:")
                        for r in written:
                            full = repo_root / r['path']
                            st.write(f"• `{full}`")
                    if errors:
                        st.error(f"❌ {len(errors)} feil:")
                        for r in errors:
                            full = repo_root / r['path']
                            st.write(f"• `{full}`: {r.get('error', 'ukjent feil')}")


                    if written and not errors:
                        st.balloons()
                except Exception as e:
                    st.error(f"❌ {e}")


# ═══════════════════════════════════════════════════════════════════════
# HISTORY (both sides)
# ═══════════════════════════════════════════════════════════════════════


with tab_history:
    st.subheader("📜 Patch-historikk")
    history = get_patch_history_summary(repo_root)

    if not history:
        st.info("Ingen historikk ennå.")
    else:
        st.caption(f"{len(history)} patch(er) registrert for **{active_repo['name']}**")
        for entry in history:
            status_icon = {"applied": "✅", "generated": "📤"}.get(entry.get("status", ""), "❓")
            with st.container(border=True):
                col1, col2 = st.columns([1, 8])
                with col1:
                    st.write(f"### {status_icon}")
                with col2:
                    st.write(f"**{entry.get('patch_id', '?')}** — {entry.get('timestamp', '?')}")
                    side = entry.get("side") or entry.get("source_side", "?")
                    st.write(
                        f"Side {side.upper()} · "
                        f"{entry.get('status', '?')} · "
                        f"{entry.get('commit_count', entry.get('change_count', '?'))} commit(er)"
                        + (f" · _{entry.get('description', '')}_" if entry.get("description") else "")
                    )
                    commits = entry.get("commits") or entry.get("changes_summary", [])
                    if commits:
                        with st.expander("Commits"):
                            for c in commits:
                                if "hash" in c:
                                    st.write(f"• `{c['hash']}` {c.get('message', '')}")
                                else:
                                    st.write(f"• {c.get('action', '?')} `{c.get('file', '?')}`")

