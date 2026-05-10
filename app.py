"""Code Mover — Streamlit UI for safe patch-based code transfer.

Supports:
- Multi-repo management with config
- Two-sided workflow (generate patch on side A, apply on side B)
- Tree fingerprint validation for cross-side safety
- Tamper detection (file hash comparison)
- Patch history with before/after snapshots
- ZIP export/import for easy transfer
"""

import streamlit as st
from pathlib import Path
import yaml

from core import (
    find_repo_root,
    validate_patch,
    build_preview_diff,
    apply_patch,
    generate_patch,
    generate_changes_from_zip_diff,
    compute_tree_fingerprint,
    compute_file_structure_snapshot,
    compute_file_hashes,
    detect_changes_since_last_patch,
    validate_repo_markers,
    export_patch_to_zip,
    import_patch_from_zip,
    get_patch_history_summary,
    add_patch_to_history,
)
from config import (
    load_config,
    save_config,
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
st.caption("Safe patch-based code transfer for isolated environments")


# ─── Initialize session state ──────────────────────────────────────────

if "validated_patch" not in st.session_state:
    st.session_state["validated_patch"] = None
if "generated_patch" not in st.session_state:
    st.session_state["generated_patch"] = None
if "change_list" not in st.session_state:
    st.session_state["change_list"] = []
if "pending_changes" not in st.session_state:
    st.session_state["pending_changes"] = None
if "repo_form_name" not in st.session_state:
    st.session_state["repo_form_name"] = ""
if "repo_form_path" not in st.session_state:
    st.session_state["repo_form_path"] = ""


# ─── Sidebar: Configuration ────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Konfigurasjon")

    config = load_config()

    # ── Side selector with clear description ──
    current_side = config.get("side", "a")

    side_labels = {
        "a": "📤 **Side A — Avsender**\nGenerer patcher for overføring til B",
        "b": "📥 **Side B — Mottaker**\nMottar og applicerer patcher fra A",
    }

    st.markdown("### 🔀 Hvilken side er dette?")
    st.caption(
        "**Side A** har tilgang til LLM/eksterne verktøy og genererer patcher.\n\n"
        "**Side B** er det isolerte miljøet som mottar og applicerer patcher."
    )

    new_side = st.radio(
        "Velg side",
        options=["a", "b"],
        format_func=lambda x: side_labels[x],
        index=0 if current_side == "a" else 1,
        key="side_selector",
    )

    if new_side != current_side:
        # Safety: warn when switching from B (receiver) to A (sender)
        if current_side == "b" and new_side == "a":
            st.warning(
                "⚠️ **OBS!** Du bytter fra **mottaker (B)** til **avsender (A)**.\n\n"
                "Før du genererer nye patcher, må du være sikker på at repoet på "
                "denne siden er **fullstendig i sync** med det som ble sendt til B. "
                "Hvis ikke vil patcher du genererer være basert på utdatert kode."
            )
            col_confirm_yes, col_confirm_no = st.columns(2)
            with col_confirm_yes:
                if st.button("✅ Ja, jeg bekrefter sync", use_container_width=True):
                    config = set_side(new_side)
                    st.rerun()
            with col_confirm_no:
                if st.button("❌ Avbryt", use_container_width=True):
                    st.rerun()
        else:
            config = set_side(new_side)
            st.rerun()

    st.divider()

    # Active repo selector
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

        selected_repo = st.selectbox(
            "Velg repo",
            options=repo_names,
            index=active_index,
            key="repo_selector",
        )

        if selected_repo != (active_repo["name"] if active_repo else None):
            set_active_repo(selected_repo)
            st.rerun()
    else:
        st.info("Ingen repoer registrert. Legg til et repo nedenfor.")

    # Show active repo info
    active_repo = get_active_repo()
    if active_repo:
        repo_path = Path(active_repo["path"])
        st.success(f"✅ Aktivt: **{active_repo['name']}**")
        st.caption(f"`{repo_path}`")

        # Validate markers
        missing = validate_repo_markers(
            repo_path, active_repo.get("markers", ["app.py", "core.py"])
        )
        if missing:
            st.warning(f"⚠️ Mangler markører: {', '.join(missing)}")
        else:
            st.caption("✅ Alle markører funnet")

        # Show fingerprint
        fp = compute_tree_fingerprint(repo_path)
        st.caption(f"🔑 Fingeravtrykk: `{fp}`")

        # Tamper detection: check for changes since last patch
        changes_since = detect_changes_since_last_patch(repo_path)
        if changes_since:
            with st.expander("⚠️ Endringer siden siste patch", expanded=True):
                for c in changes_since:
                    icon = {"new": "🆕", "modified": "✏️", "deleted": "🗑️"}.get(
                        c["type"], "❓"
                    )
                    st.write(f"{icon} `{c['file']}` ({c['type']})")
        else:
            st.caption("✅ Ingen endringer siden siste patch")

    st.divider()

    # Repo management expander
    with st.expander("➕ Legg til / fjern repo"):
        if st.button("🔍 Søk etter repoer", use_container_width=True):
            with st.spinner("Søker..."):
                found = discover_repos()
            if found:
                st.success(f"Fant {len(found)} repo(er)")
                for r in found:
                    col_a, col_b, col_c = st.columns([2, 3, 1])
                    with col_a:
                        git_icon = "📁" if r.get("has_git") else "📂"
                        st.write(f"{git_icon} **{r['name']}**")
                    with col_b:
                        st.caption(f"`{r['path']}`")
                    with col_c:
                        if st.button(f"✅ Legg til", key=f"add_{r['name']}"):
                            add_repo(r["name"], r["path"])
                            set_active_repo(r["name"])
                            st.rerun()
            else:
                st.warning("Ingen repoer funnet. Prøv å legge til manuelt.")

        st.divider()

        with st.form("add_repo_form"):
            prefill_name = st.session_state.get("repo_form_name", "")
            prefill_path = st.session_state.get("repo_form_path", "")

            new_name = st.text_input(
                "Repo-navn",
                value=prefill_name,
                placeholder="F.eks. fleet-manager",
            )
            new_path = st.text_input(
                "Sti (absolutt)",
                value=prefill_path,
                placeholder="F.eks. /Users/bruker/Documents/GitHub/fleet-manager",
            )
            new_markers = st.text_input(
                "Markører (kommaseparert)", value="app.py, core.py"
            )

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
            repo_to_remove = st.selectbox(
                "Fjern repo",
                options=[""] + repo_names,
                key="remove_repo_selector",
            )
            if repo_to_remove and st.button(
                "🗑️ Fjern", type="secondary", use_container_width=True
            ):
                remove_repo(repo_to_remove)
                st.rerun()


# ─── Main content area ─────────────────────────────────────────────────

# Check that we have an active repo
active_repo = get_active_repo()
if not active_repo:
    st.warning(
        "⚠️ Ingen repo valgt. "
        "Gå til sidemenyen for å legge til og velge et repo."
    )
    st.stop()

repo_root = Path(active_repo["path"])
current_side = get_side()

# Show different tabs depending on which side we are
if current_side == "a":
    # Side A (sender): Generate patch from diff + History
    tab_status, tab_history = st.tabs(
        ["📤 Generer patch fra diff", "📜 Historikk"]
    )
else:
    # Side B (receiver): Apply patch + History
    tab_apply, tab_history = st.tabs(
        ["📥 Apply patch", "📜 Historikk"]
    )


# ═══════════════════════════════════════════════════════════════════════
# TAB: APPLY PATCH (only on Side B)
# ═══════════════════════════════════════════════════════════════════════

if current_side == "b":
    with tab_apply:
        st.subheader("📥 Apply patch fra den andre siden")

        st.info(
            f"Du er på **side {current_side.upper()}**. "
            f"Lim inn en patch generert på side A."
        )

        # Two input methods: paste YAML or upload ZIP
        input_method = st.radio(
            "Inndatametode",
            options=["📋 Lim inn YAML", "📁 Last opp ZIP"],
            horizontal=True,
        )

        patch_text = ""
        source_file_hashes = None
        zip_metadata = None

        if input_method == "📋 Lim inn YAML":
            patch_text = st.text_area(
                "📋 Lim inn YAML-patch",
                height=300,
                placeholder="Lim inn YAML-patch her...",
                key="apply_patch_input",
            )
        else:
            uploaded_file = st.file_uploader(
                "📁 Last opp .zip-fil",
                type=["zip"],
                help="Last opp en ZIP-fil generert fra den andre siden",
            )
            if uploaded_file is not None:
                try:
                    zip_bytes = uploaded_file.getvalue()
                    patch_text, source_file_hashes, zip_metadata = import_patch_from_zip(
                        zip_bytes
                    )

                    # Show ZIP metadata
                    with st.expander("📄 ZIP-metadata", expanded=True):
                        st.json(zip_metadata)

                    st.success(
                        f"✅ ZIP lastet inn: {len(patch_text)} bytes YAML, "
                        f"{len(source_file_hashes)} filhasher"
                    )
                except Exception as e:
                    st.error(f"❌ Kunne ikke lese ZIP-fil: {e}")

        col_validate, col_apply = st.columns(2)

        with col_validate:
            if st.button("🔍 Valider patch", use_container_width=True):
                if not patch_text.strip():
                    st.error("❌ Lim inn en patch eller last opp ZIP først")
                else:
                    try:
                        patch = validate_patch(
                            patch_text,
                            repo_root,
                            expected_side=current_side,
                            source_file_hashes=source_file_hashes,
                        )
                        diff = build_preview_diff(patch, repo_root)

                        st.session_state["validated_patch"] = patch

                        # Show patch metadata
                        with st.expander("📄 Patch-metadata", expanded=True):
                            st.write(f"**Kilde-side:** {patch.get('source_side', '?')}")
                            st.write(f"**Generert:** {patch.get('generated_at', '?')}")
                            st.write(
                                f"**Beskrivelse:** {patch.get('description', '—')}"
                            )
                            st.write(
                                f"**Fingeravtrykk:** `{patch.get('source_tree_fingerprint', '?')}`"
                            )
                            st.write(
                                f"**Endringer:** {len(patch.get('changes', []))} stk"
                            )

                        # Show tree structure for visual verification
                        tree = patch.get("source_tree_structure", "")
                        if tree:
                            with st.expander("🌳 Kilde-repo trestruktur"):
                                st.code(tree, language="")

                        # Show diff
                        st.code(diff, language="diff")
                        st.success("✅ Patch validert. Klar til apply.")

                    except Exception as e:
                        st.error(f"❌ Validering feilet: {e}")
                        st.session_state["validated_patch"] = None

        with col_apply:
            if st.button(
                "🚀 Apply patch", use_container_width=True, type="primary"
            ):
                patch = st.session_state.get("validated_patch")

                if not patch:
                    st.error("❌ Patch må valideres først.")
                else:
                    try:
                        apply_patch(patch, repo_root)
                        st.success("✅ Patch applied!")
                        st.balloons()
                        st.session_state["validated_patch"] = None
                    except Exception as e:
                        st.error(f"❌ Apply feilet: {e}")


# ═══════════════════════════════════════════════════════════════════════
# TAB: GENERER FRA DIFF (only on Side A)
# ═══════════════════════════════════════════════════════════════════════

if current_side == "a":
    with tab_status:
        st.subheader("📤 Generer patch for overføring til B")

        # ── Option 1: Upload baseline ZIP ──
        uploaded_baseline_zip = st.file_uploader(
            "📁 Last opp baseline-ZIP (valgfritt)",
            type=["zip"],
            key="baseline_zip_uploader",
            help="Last opp en ZIP-fil (tidligere eksportert fra Code Mover) "
                 "for å sammenligne med repoet. Uten ZIP sendes alle filer.",
        )

        changes = None
        patch_description = ""

        if uploaded_baseline_zip is not None:
            # ── With ZIP: generate diff-based changes ──
            try:
                zip_bytes = uploaded_baseline_zip.getvalue()

                # Show ZIP metadata
                try:
                    _, _, zip_metadata = import_patch_from_zip(zip_bytes)
                    with st.expander("📄 ZIP-metadata", expanded=True):
                        st.json(zip_metadata)
                except Exception:
                    pass

                # Generate changes from diff
                with st.spinner("🔍 Sammenligner filer..."):
                    changes = generate_changes_from_zip_diff(zip_bytes, repo_root)

                if not changes:
                    st.success("✅ Ingen forskjeller funnet — repoet er identisk med ZIP-en.")
                else:
                    st.success(f"✅ Fant {len(changes)} endring(er) basert på diff mot ZIP")

                    # Show changes summary
                    st.write("**Endringer som vil bli inkludert:**")
                    for i, c in enumerate(changes):
                        icon = {
                            "patch_hunk": "✏️",
                            "create_file": "🆕",
                        }.get(c["action"], "❓")
                        st.write(f"{icon} `{c['action']}` — `{c['file']}`")

            except Exception as e:
                st.error(f"❌ Kunne ikke prosessere ZIP-fil: {e}")

        else:
            # ── No ZIP: ask if user wants to send everything ──
            st.warning(
                "⚠️ **Ingen baseline-ZIP lastet opp.**\n\n"
                "Det finnes ingen status fra den andre siden å sammenligne med. "
                "Er alle endringer i repoet ment å sendes?"
            )

            if st.button("✅ Ja, send alle filer som patch", use_container_width=True):
                # Generate a patch with ALL files as create_file
                all_hashes = compute_file_hashes(repo_root)
                all_changes = []
                for rel_path in sorted(all_hashes.keys()):
                    full_path = repo_root / rel_path
                    if full_path.exists() and full_path.is_file():
                        content = full_path.read_text(encoding="utf-8", errors="replace")
                        all_changes.append({
                            "file": rel_path,
                            "action": "create_file",
                            "content": content,
                        })

                if all_changes:
                    changes = all_changes
                    st.success(f"✅ Klar til å sende {len(changes)} fil(er)")
                else:
                    st.error("❌ Ingen filer funnet i repoet")

        # ── Common: description + generate button ──
        if changes is not None:
            patch_description = st.text_input(
                "📝 Beskrivelse (valgfritt)",
                placeholder="F.eks. 'Oppdatert etter tilbakemelding fra B'",
                key="diff_description",
            )

            if st.button(
                "📤 Generer patch",
                use_container_width=True,
                type="primary",
            ):
                patch_yaml = generate_patch(
                    changes=changes,
                    repo_root=repo_root,
                    side=current_side,
                    description=patch_description,
                )
                st.session_state["generated_patch"] = patch_yaml

                # Record in history
                patch = yaml.safe_load(patch_yaml)
                add_patch_to_history(
                    repo_root,
                    patch,
                    status="generated",
                )

                st.success("✅ Patch generert!")
                st.rerun()

        # Show generated patch if any
        if st.session_state.get("generated_patch"):
            st.divider()
            st.subheader("✅ Generert patch")

            patch_yaml = st.session_state["generated_patch"]

            st.info(
                "💡 Overfør denne patchen til den andre siden. "
                "Du kan enten kopiere YAML-en direkte, eller laste ned som ZIP."
            )

            with st.expander("📋 Vis YAML", expanded=False):
                st.code(patch_yaml, language="yaml")

            col_yaml, col_zip = st.columns(2)

            with col_yaml:
                st.download_button(
                    label="💾 Last ned som .yaml",
                    data=patch_yaml,
                    file_name=f"patch_{current_side}_to_b_{Path(repo_root.name)}.yaml",
                    mime="text/yaml",
                    use_container_width=True,
                )

            with col_zip:
                zip_bytes = export_patch_to_zip(patch_yaml, repo_root)
                st.download_button(
                    label="📦 Last ned som .zip (anbefalt)",
                    data=zip_bytes,
                    file_name=f"patch_{current_side}_to_b_{Path(repo_root.name)}.zip",
                    mime="application/zip",
                    use_container_width=True,
                )


# ═══════════════════════════════════════════════════════════════════════
# TAB: HISTORY
# ═══════════════════════════════════════════════════════════════════════

with tab_history:
    st.subheader("📜 Patch-historikk")

    history = get_patch_history_summary(repo_root)

    if not history:
        st.info("Ingen patch-historikk ennå. Generer eller apply en patch for å komme i gang.")
    else:
        st.write(f"**{len(history)} patcher registrert** for `{active_repo['name']}`")

        for entry in history:
            status_icon = {
                "applied": "✅",
                "generated": "📤",
                "failed": "❌",
            }.get(entry["status"], "❓")

            with st.container(border=True):
                col1, col2 = st.columns([1, 5])
                with col1:
                    st.write(f"### {status_icon}")
                with col2:
                    st.write(
                        f"**{entry['patch_id']}** — {entry['timestamp']}"
                    )
                    st.write(
                        f"Side {entry['source_side'].upper()} → "
                        f"{entry['status']} · "
                        f"{entry['change_count']} endring(er) · "
                        f"_{entry['description']}_"
                    )

                    # Show changes summary
                    with st.expander("📋 Vis endringer"):
                        for c in entry["changes_summary"]:
                            st.write(f"- `{c['action']}` — `{c['file']}`")
