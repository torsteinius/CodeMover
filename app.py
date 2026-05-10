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

import streamlit as st
from pathlib import Path
from datetime import datetime

from core import (
    # Repo
    find_repo_root,
    validate_repo_markers,
    check_is_git_repo,
    compute_file_structure_snapshot,
    # Git state
    get_current_commit,
    get_commits_since,
    get_uncommitted_files,
    # Sync state
    load_sync_state,
    save_sync_state,
    # Patch generation / application
    generate_format_patch,
    preview_format_patch,
    apply_format_patch,
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

st.set_page_config(page_title="Code Mover", layout="wide")
st.title("📦 Code Mover")
st.caption("Git-based code transfer for isolated environments")


# ─── Session state ──────────────────────────────────────────────────────

if "generated_patch" not in st.session_state:
    st.session_state["generated_patch"] = None      # raw patch text
if "generated_meta" not in st.session_state:
    st.session_state["generated_meta"] = {}         # metadata dict
if "repo_form_name" not in st.session_state:
    st.session_state["repo_form_name"] = ""
if "repo_form_path" not in st.session_state:
    st.session_state["repo_form_path"] = ""


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
                if st.button("✅ Bekreft", use_container_width=True):
                    set_side(new_side)
                    st.rerun()
            with col_no:
                if st.button("❌ Avbryt", use_container_width=True):
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
        if st.button("🔍 Søk etter repoer", use_container_width=True):
            with st.spinner("Søker..."):
                found = discover_repos()
            if found:
                st.success(f"Fant {len(found)} repo(er)")
                for r in found:
                    col_a, col_b, col_c = st.columns([2, 3, 1])
                    with col_a:
                        st.write(f"{'📁' if r.get('has_git') else '📂'} **{r['name']}**")
                    with col_b:
                        st.caption(f"`{r['path']}`")
                    with col_c:
                        if st.button("✅", key=f"add_{r['name']}"):
                            add_repo(r["name"], r["path"])
                            set_active_repo(r["name"])
                            st.rerun()
            else:
                st.warning("Ingen repoer funnet. Legg til manuelt.")

        st.divider()

        with st.form("add_repo_form"):
            new_name = st.text_input(
                "Repo-navn",
                value=st.session_state.get("repo_form_name", ""),
                placeholder="fleet-manager",
            )
            new_path = st.text_input(
                "Sti (absolutt)",
                value=st.session_state.get("repo_form_path", ""),
                placeholder="/Users/bruker/Documents/GitHub/fleet-manager",
            )
            new_markers = st.text_input("Markører (kommaseparert)", value="app.py, core.py")
            if st.form_submit_button("💾 Lagre repo", use_container_width=True):
                if new_name and new_path:
                    markers = [m.strip() for m in new_markers.split(",") if m.strip()]
                    add_repo(new_name, new_path, markers)
                    st.session_state["repo_form_name"] = ""
                    st.session_state["repo_form_path"] = ""
                    st.rerun()
                else:
                    st.error("Fyll inn navn og sti")

        if repo_names:
            st.divider()
            to_remove = st.selectbox("Fjern repo", options=[""] + repo_names, key="remove_selector")
            if to_remove and st.button("🗑️ Fjern", type="secondary", use_container_width=True):
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
    tab_generate, tab_history = st.tabs(["📤 Generer patch", "📜 Historikk"])
else:
    tab_apply, tab_history = st.tabs(["📥 Apply patch", "📜 Historikk"])


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

        if st.button("📤 Generer patch", type="primary", use_container_width=True):
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

            tab_copy, tab_zip, tab_file = st.tabs(
                ["📋 Kopier tekst", "📦 Last ned ZIP", "💾 Last ned .patch-fil"]
            )

            with tab_copy:
                st.caption("Merk alt og kopier. Lim inn i **Lim inn tekst**-fanen på Side B.")
                st.text_area(
                    "patch",
                    value=patch_text,
                    height=400,
                    label_visibility="collapsed",
                )

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

        # ── Preview + Apply ─────────────────────────────────────────────
        if patch_text.strip():
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

            if st.button("🚀 Apply patch", type="primary", use_container_width=True):
                try:
                    with st.spinner("Kjører git am..."):
                        output = apply_format_patch(repo_root, patch_text)

                    # Find the new HEAD after apply
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
            status_icon = {"applied": "✅", "generated": "📤"}.get(entry["status"], "❓")
            with st.container(border=True):
                col1, col2 = st.columns([1, 8])
                with col1:
                    st.write(f"### {status_icon}")
                with col2:
                    st.write(f"**{entry['patch_id']}** — {entry['timestamp']}")
                    st.write(
                        f"Side {entry['side'].upper()} · "
                        f"{entry['status']} · "
                        f"{entry['commit_count']} commit(er)"
                        + (f" · _{entry['description']}_" if entry.get("description") else "")
                    )
                    with st.expander("Commits"):
                        for c in entry.get("commits", []):
                            st.write(f"• `{c['hash']}` {c['message']}")
