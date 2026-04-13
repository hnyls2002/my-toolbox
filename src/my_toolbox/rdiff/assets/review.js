(function () {
  const PATH_KEY = location.pathname;
  const FILE_KEY = "d2h_viewed_file_" + PATH_KEY;
  const BLOCK_KEY = "d2h_reviewed_block_" + PATH_KEY;
  const REPO_ROOT = "__REPO_ROOT__";
  const EDITOR_KEY = "d2h_editor_scheme"; // 'cursor' or 'vscode'

  function editorScheme() {
    return localStorage.getItem(EDITOR_KEY) || "cursor";
  }
  function editorURL(relPath, line) {
    const abs =
      REPO_ROOT.replace(/\/$/, "") + "/" + relPath.replace(/^\//, "");
    const suffix = line ? ":" + line : "";
    return editorScheme() + "://file" + abs + suffix;
  }

  function getJSON(k) {
    try {
      return JSON.parse(localStorage.getItem(k) || "{}");
    } catch {
      return {};
    }
  }
  function setJSON(k, v) {
    localStorage.setItem(k, JSON.stringify(v));
  }

  function fileNameFromWrapper(wrap) {
    const n = wrap.querySelector(".d2h-file-name");
    return n ? n.textContent.trim() : "?";
  }

  function attachFileCheckboxes() {
    document.querySelectorAll(".d2h-file-collapse-input").forEach((cb) => {
      if (cb.dataset.d2hBound) return;
      cb.dataset.d2hBound = "1";
      cb.addEventListener("change", () => {
        const wrap = cb.closest(".d2h-file-wrapper");
        const key = fileNameFromWrapper(wrap);
        const st = getJSON(FILE_KEY);
        if (cb.checked) st[key] = 1;
        else delete st[key];
        setJSON(FILE_KEY, st);
        updateProgress();
      });
    });
  }

  function restoreFileCheckboxes() {
    const st = getJSON(FILE_KEY);
    document.querySelectorAll(".d2h-file-collapse-input").forEach((cb) => {
      const wrap = cb.closest(".d2h-file-wrapper");
      const key = fileNameFromWrapper(wrap);
      if (st[key] && !cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  }

  // For every diff block (consecutive +/- rows, no context lines in
  // between), insert a sub-header row carrying its own mark button. The
  // button is per-block, not per-hunk: a single hunk that contains
  // several non-adjacent +/- groups (because of large --context) gets one
  // mark button per group. Mark fades only that block's +/- rows.
  function processFileForBlocks(wrap) {
    const fname = fileNameFromWrapper(wrap);
    const tbodies = wrap.querySelectorAll("tbody");
    const perSide = [];
    tbodies.forEach((tb) => {
      const rows = Array.from(tb.children);
      const infoIdxs = [];
      rows.forEach((r, i) => {
        if (
          r.querySelector(".d2h-info") ||
          r.classList.contains("d2h-info")
        ) {
          infoIdxs.push(i);
        }
      });
      perSide.push({ tbody: tb, rows, infoIdxs });
    });

    const maxH = Math.max(0, ...perSide.map((s) => s.infoIdxs.length));

    for (let h = 0; h < maxH; h++) {
      const hunkId = fname + "::hunk" + h;

      // Step 1: tag rows on both sides (hunk header, body rows, context).
      perSide.forEach((side) => {
        const startIdx = side.infoIdxs[h];
        if (startIdx === undefined) return;
        const endIdx =
          h + 1 < side.infoIdxs.length
            ? side.infoIdxs[h + 1]
            : side.rows.length;
        const header = side.rows[startIdx];
        const bodyRows = side.rows.slice(startIdx + 1, endIdx);

        header.dataset.hunkId = hunkId;
        header.classList.add("d2h-hunk-header");

        bodyRows.forEach((r) => {
          r.dataset.hunkId = hunkId;
          r.classList.add("d2h-hunk-row");
          // A row is "pure context" (unchanged, shown both sides) only if
          // its content td has `d2h-cntx` AND NOT `d2h-emptyplaceholder`.
          // The left-side filler of an addition row also carries `d2h-cntx`
          // but is paired with a `+` on the right — it belongs to the
          // diff block, not to surrounding context.
          const contentTd = r.querySelector(
            "td:not(.d2h-code-side-linenumber):not(.d2h-code-linenumber)",
          );
          const isPureContext =
            contentTd &&
            contentTd.classList.contains("d2h-cntx") &&
            !contentTd.classList.contains("d2h-emptyplaceholder");
          if (isPureContext) r.classList.add("d2h-hunk-context");
        });
      });

      // Step 2: identify diff blocks within this hunk. We use the first
      // side's body row sequence as authoritative — both sides share the
      // same length and context/change pattern (paired by filler rows).
      const refSide = perSide[0];
      const refStart = refSide.infoIdxs[h];
      const refEnd =
        h + 1 < refSide.infoIdxs.length
          ? refSide.infoIdxs[h + 1]
          : refSide.rows.length;
      const refBody = refSide.rows.slice(refStart + 1, refEnd);

      let blockIdx = 0;
      let i = 0;
      while (i < refBody.length) {
        if (refBody[i].classList.contains("d2h-hunk-context")) {
          i++;
          continue;
        }
        const blockStart = i;
        while (
          i < refBody.length &&
          !refBody[i].classList.contains("d2h-hunk-context")
        ) {
          i++;
        }
        const blockId = hunkId + "::block" + blockIdx;

        // Tag block rows on each side (same indexes, since rows are paired).
        perSide.forEach((side) => {
          const sStart = side.infoIdxs[h];
          if (sStart === undefined) return;
          for (let j = blockStart; j < i; j++) {
            const row = side.rows[sStart + 1 + j];
            if (!row) continue;
            row.dataset.blockId = blockId;
            row.classList.add("d2h-diff-block");
          }
        });

        // Insert a sub-header row carrying the mark button, on each side.
        perSide.forEach((side) => {
          const sStart = side.infoIdxs[h];
          if (sStart === undefined) return;
          const firstBlockRow = side.rows[sStart + 1 + blockStart];
          if (!firstBlockRow || !firstBlockRow.parentNode) return;

          const sub = document.createElement("tr");
          sub.className = "d2h-block-subheader";
          sub.dataset.blockId = blockId;
          const td1 = document.createElement("td");
          td1.className = "d2h-block-subheader-cell";
          const td2 = document.createElement("td");
          td2.className = "d2h-block-subheader-cell";
          const btn = document.createElement("button");
          btn.className = "d2h-block-toggle";
          btn.dataset.blockId = blockId;
          btn.textContent = "mark";
          btn.title = "Toggle reviewed";
          btn.onclick = (e) => {
            e.stopPropagation();
            toggleBlock(blockId);
          };
          td2.appendChild(btn);
          sub.appendChild(td1);
          sub.appendChild(td2);
          firstBlockRow.parentNode.insertBefore(sub, firstBlockRow);
        });

        blockIdx++;
      }
    }
  }

  function toggleBlock(blockId) {
    const st = getJSON(BLOCK_KEY);
    if (st[blockId]) delete st[blockId];
    else st[blockId] = 1;
    setJSON(BLOCK_KEY, st);
    applyBlockState();
    updateProgress();
  }

  function applyBlockState() {
    const st = getJSON(BLOCK_KEY);
    document
      .querySelectorAll(".d2h-diff-block, .d2h-block-subheader")
      .forEach((r) => {
        const id = r.dataset.blockId;
        if (st[id]) r.classList.add("d2h-block-reviewed");
        else r.classList.remove("d2h-block-reviewed");
      });
    document.querySelectorAll(".d2h-block-toggle").forEach((btn) => {
      const id = btn.dataset.blockId;
      if (st[id]) {
        btn.classList.add("is-reviewed");
        btn.textContent = "done";
      } else {
        btn.classList.remove("is-reviewed");
        btn.textContent = "mark";
      }
    });
  }

  function countBlocks() {
    const ids = new Set();
    document
      .querySelectorAll(".d2h-block-toggle")
      .forEach((b) => ids.add(b.dataset.blockId));
    return ids.size;
  }
  function countReviewedBlocks() {
    const ids = new Set();
    document
      .querySelectorAll(".d2h-block-toggle.is-reviewed")
      .forEach((b) => ids.add(b.dataset.blockId));
    return ids.size;
  }
  function countFiles() {
    return document.querySelectorAll(".d2h-file-wrapper").length;
  }
  function countViewedFiles() {
    return Array.from(
      document.querySelectorAll(".d2h-file-collapse-input"),
    ).filter((c) => c.checked).length;
  }

  function updateProgress() {
    const ft = countFiles(),
      fv = countViewedFiles();
    const bt = countBlocks(),
      bv = countReviewedBlocks();
    const fp = ft ? Math.round((fv * 100) / ft) : 0;
    const bp = bt ? Math.round((bv * 100) / bt) : 0;
    const elF = document.getElementById("d2h-file-progress");
    const elB = document.getElementById("d2h-block-progress");
    if (elF) elF.textContent = "Files: " + fv + "/" + ft + " (" + fp + "%)";
    if (elB) elB.textContent = "Diffs: " + bv + "/" + bt + " (" + bp + "%)";
  }

  function openInEditor(url) {
    console.log("[rdiff] opening:", url);
    const a = document.createElement("a");
    a.href = url;
    a.target = "_self";
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 0);
    showFlash(url);
  }

  function showFlash(text) {
    let flash = document.getElementById("d2h-flash");
    if (!flash) {
      flash = document.createElement("div");
      flash.id = "d2h-flash";
      flash.style.cssText =
        "position:fixed;bottom:16px;left:50%;transform:translateX(-50%);" +
        "background:rgba(0,0,0,0.85);color:#fff;padding:8px 14px;border-radius:6px;" +
        "z-index:10000;font-family:monospace;font-size:12px;max-width:80vw;" +
        "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      document.body.appendChild(flash);
    }
    flash.textContent = text;
    flash.style.opacity = "1";
    clearTimeout(showFlash._t);
    showFlash._t = setTimeout(() => {
      flash.style.opacity = "0";
      flash.style.transition = "opacity 0.4s";
    }, 1800);
  }

  function attachEditorLinks() {
    document.querySelectorAll(".d2h-file-name").forEach((el) => {
      if (el.dataset.d2hLinkBound) return;
      el.dataset.d2hLinkBound = "1";
      el.classList.add("d2h-cursor-link");
      el.title =
        "Click: open in " +
        editorScheme() +
        " - Shift+Click: copy URL";
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        e.preventDefault();
        const rel = el.textContent.trim();
        const url = editorURL(rel, null);
        if (e.shiftKey) {
          navigator.clipboard.writeText(url);
          showFlash("copied: " + url);
          return;
        }
        openInEditor(url);
      });
    });

    // Side-by-side: <td class="d2h-code-side-linenumber">NN</td> (plain text).
    // Line-by-line: <td class="d2h-code-linenumber"><div class="line-num1">...</div><div class="line-num2">...</div></td>.
    document
      .querySelectorAll(".d2h-code-linenumber, .d2h-code-side-linenumber")
      .forEach((el) => {
        if (el.dataset.d2hLineBound) return;
        el.dataset.d2hLineBound = "1";
        function extractLineNum(cell) {
          const direct = (cell.textContent || "").trim();
          if (/^\d+$/.test(direct)) return direct;
          const n2 = cell.querySelector(".line-num2");
          const n1 = cell.querySelector(".line-num1");
          const rn = n2 ? n2.textContent.trim() : "";
          const ro = n1 ? n1.textContent.trim() : "";
          if (/^\d+$/.test(rn)) return rn;
          if (/^\d+$/.test(ro)) return ro;
          return null;
        }
        const useNum = extractLineNum(el);
        if (!useNum) return;
        el.classList.add("d2h-cursor-clickable");
        el.title =
          "Click: open line " +
          useNum +
          " in " +
          editorScheme() +
          " - Shift+Click: copy URL";
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          const wrap = el.closest(".d2h-file-wrapper");
          if (!wrap) return;
          const rel = fileNameFromWrapper(wrap);
          const url = editorURL(rel, useNum);
          if (e.shiftKey) {
            navigator.clipboard.writeText(url);
            showFlash("copied: " + url);
            return;
          }
          openInEditor(url);
        });
      });
  }

  function addToolbar() {
    const bar = document.createElement("div");
    bar.id = "d2h-toolbar";
    bar.innerHTML =
      '<div id="d2h-file-progress" class="pill" style="background:#3572b0;"></div>' +
      '<div id="d2h-block-progress" class="pill" style="background:#6b46c1;"></div>' +
      '<select id="d2h-editor">' +
      '<option value="cursor">Cursor</option>' +
      '<option value="vscode">VS Code</option>' +
      "</select>" +
      '<button id="d2h-hide-blocks" style="background:#2ea043;">Hide reviewed diffs</button>' +
      '<button id="d2h-hide-files" style="background:#2ea043;">Hide viewed files</button>' +
      '<button id="d2h-next" style="background:#3572b0;">Next unreviewed</button>' +
      '<button id="d2h-test-editor" style="background:#6b46c1;">Test editor</button>' +
      '<button id="d2h-clear-blocks" style="background:#c33;">Clear diffs</button>' +
      '<button id="d2h-clear-files" style="background:#c33;">Clear files</button>';
    document.body.appendChild(bar);

    const editorSel = document.getElementById("d2h-editor");
    editorSel.value = editorScheme();
    editorSel.onchange = () => {
      localStorage.setItem(EDITOR_KEY, editorSel.value);
      document
        .querySelectorAll(".d2h-file-name.d2h-cursor-link")
        .forEach((el) => {
          el.title = "Open in " + editorSel.value;
        });
      document.querySelectorAll(".d2h-cursor-clickable").forEach((el) => {
        const n = el.title.match(/line (\d+)/);
        el.title =
          "Open in " + editorSel.value + (n ? " at line " + n[1] : "");
      });
    };

    let blocksHidden = false;
    document.getElementById("d2h-hide-blocks").onclick = () => {
      blocksHidden = !blocksHidden;
      document.getElementById("d2h-hide-blocks").textContent = blocksHidden
        ? "Show reviewed diffs"
        : "Hide reviewed diffs";
      document
        .querySelectorAll(".d2h-diff-block, .d2h-block-subheader")
        .forEach((r) => {
          if (!r.classList.contains("d2h-block-reviewed")) return;
          r.classList.toggle("d2h-block-hidden", blocksHidden);
        });
    };

    let filesHidden = false;
    document.getElementById("d2h-hide-files").onclick = () => {
      filesHidden = !filesHidden;
      document.getElementById("d2h-hide-files").textContent = filesHidden
        ? "Show viewed files"
        : "Hide viewed files";
      document.querySelectorAll(".d2h-file-collapse-input").forEach((cb) => {
        const w = cb.closest(".d2h-file-wrapper");
        if (w) w.style.display = filesHidden && cb.checked ? "none" : "";
      });
    };

    document.getElementById("d2h-next").onclick = () => {
      const subs = Array.from(
        document.querySelectorAll(".d2h-block-subheader"),
      );
      const seen = new Set();
      const first = subs.find((s) => {
        if (seen.has(s.dataset.blockId)) return false;
        seen.add(s.dataset.blockId);
        return !s.classList.contains("d2h-block-reviewed");
      });
      if (first)
        first.scrollIntoView({ behavior: "smooth", block: "center" });
    };

    document.getElementById("d2h-clear-blocks").onclick = () => {
      if (!confirm("Clear all diff review state?")) return;
      localStorage.removeItem(BLOCK_KEY);
      location.reload();
    };
    document.getElementById("d2h-clear-files").onclick = () => {
      if (!confirm("Clear all file viewed state?")) return;
      localStorage.removeItem(FILE_KEY);
      location.reload();
    };
    document.getElementById("d2h-test-editor").onclick = () => {
      const first = document.querySelector(".d2h-file-name");
      if (!first) {
        alert("No files found.");
        return;
      }
      const rel = first.textContent.trim();
      openInEditor(editorURL(rel, "1"));
    };
  }

  function init() {
    document
      .querySelectorAll(".d2h-file-wrapper")
      .forEach(processFileForBlocks);
    attachFileCheckboxes();
    attachEditorLinks();
    addToolbar();
    applyBlockState();
    restoreFileCheckboxes();
    updateProgress();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
