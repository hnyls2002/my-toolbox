(function () {
  const PATH_KEY = location.pathname;
  const FILE_KEY = "d2h_viewed_file_" + PATH_KEY;
  const HUNK_KEY = "d2h_viewed_hunk_" + PATH_KEY;
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

  function processFileForHunks(wrap) {
    const fname = fileNameFromWrapper(wrap);
    const tbodies = wrap.querySelectorAll("tbody");
    // Each tbody is one "side" in side-by-side mode (just one tbody in
    // line-by-line mode). Hunk rows on both sides need the hunk-id data
    // attribute so the CSS highlight/hide applies to both halves.
    // BUT the visible `mark` button is only placed on rows whose info cell
    // actually carries the `@@ ... @@` text — on the opposite side the
    // info cell is a `&nbsp;` filler placeholder.
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
      perSide.forEach((side) => {
        const startIdx = side.infoIdxs[h];
        if (startIdx === undefined) return;
        const endIdx =
          h + 1 < side.infoIdxs.length
            ? side.infoIdxs[h + 1]
            : side.rows.length;
        const header = side.rows[startIdx];
        const bodyRows = side.rows.slice(startIdx + 1, endIdx);

        bodyRows.forEach((r) => {
          r.dataset.hunkId = hunkId;
          r.classList.add("d2h-hunk-row");
        });
        header.dataset.hunkId = hunkId;
        header.classList.add("d2h-hunk-header");

        // A hunk-header row has two cells with `d2h-info`: the line-number
        // td and the content td (one of them holds the `@@ -X,Y +A,B @@`
        // text, or `&nbsp;` on the filler side). Pick the content td (the
        // non-line-number one), so the button sits next to the `@@` text
        // when that side has it.
        const infoCells = Array.from(header.querySelectorAll(".d2h-info"));
        const contentCell = infoCells.find(
          (c) =>
            !c.classList.contains("d2h-code-side-linenumber") &&
            !c.classList.contains("d2h-code-linenumber"),
        );
        if (!contentCell || contentCell.querySelector(".d2h-hunk-toggle")) {
          return;
        }

        // `isRealHeader` = this side actually has the `@@ ...` text; the
        // opposite side has `&nbsp;` placeholder. We add a button to both
        // sides (so row heights stay aligned), but hide the filler one.
        const txt = (contentCell.textContent || "").trim();
        const isRealHeader = txt.startsWith("@@");

        const btn = document.createElement("button");
        btn.className =
          "d2h-hunk-toggle" + (isRealHeader ? "" : " is-filler");
        btn.dataset.hunkId = hunkId;
        btn.textContent = "mark";
        btn.title = "Toggle reviewed";
        btn.onclick = (e) => {
          e.stopPropagation();
          toggleHunk(hunkId);
        };
        // `aria-hidden` and disabled for the filler side so it doesn't
        // mistakenly intercept clicks or screen readers.
        if (!isRealHeader) {
          btn.setAttribute("aria-hidden", "true");
          btn.setAttribute("tabindex", "-1");
        }
        contentCell.appendChild(btn);
      });
    }
  }

  function toggleHunk(hunkId) {
    const st = getJSON(HUNK_KEY);
    if (st[hunkId]) delete st[hunkId];
    else st[hunkId] = 1;
    setJSON(HUNK_KEY, st);
    applyHunkState();
    updateProgress();
  }

  function applyHunkState() {
    const st = getJSON(HUNK_KEY);
    document
      .querySelectorAll(".d2h-hunk-row, .d2h-hunk-header")
      .forEach((r) => {
        const id = r.dataset.hunkId;
        if (st[id]) r.classList.add("d2h-hunk-reviewed");
        else r.classList.remove("d2h-hunk-reviewed");
      });
    document.querySelectorAll(".d2h-hunk-toggle").forEach((btn) => {
      const id = btn.dataset.hunkId;
      if (st[id]) {
        btn.classList.add("is-reviewed");
        btn.textContent = "done";
      } else {
        btn.classList.remove("is-reviewed");
        btn.textContent = "mark";
      }
    });
  }

  function countHunks() {
    const ids = new Set();
    document
      .querySelectorAll(".d2h-hunk-toggle")
      .forEach((b) => ids.add(b.dataset.hunkId));
    return ids.size;
  }
  function countReviewedHunks() {
    const ids = new Set();
    document
      .querySelectorAll(".d2h-hunk-toggle.is-reviewed")
      .forEach((b) => ids.add(b.dataset.hunkId));
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
    const ht = countHunks(),
      hv = countReviewedHunks();
    const fp = ft ? Math.round((fv * 100) / ft) : 0;
    const hp = ht ? Math.round((hv * 100) / ht) : 0;
    const elF = document.getElementById("d2h-file-progress");
    const elH = document.getElementById("d2h-hunk-progress");
    if (elF) elF.textContent = "Files: " + fv + "/" + ft + " (" + fp + "%)";
    if (elH) elH.textContent = "Hunks: " + hv + "/" + ht + " (" + hp + "%)";
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
      '<div id="d2h-hunk-progress" class="pill" style="background:#6b46c1;"></div>' +
      '<select id="d2h-editor">' +
      '<option value="cursor">Cursor</option>' +
      '<option value="vscode">VS Code</option>' +
      "</select>" +
      '<button id="d2h-hide-hunks" style="background:#2ea043;">Hide reviewed hunks</button>' +
      '<button id="d2h-hide-files" style="background:#2ea043;">Hide viewed files</button>' +
      '<button id="d2h-next" style="background:#3572b0;">Next unreviewed</button>' +
      '<button id="d2h-test-editor" style="background:#6b46c1;">Test editor</button>' +
      '<button id="d2h-clear-hunks" style="background:#c33;">Clear hunks</button>' +
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

    let hunksHidden = false;
    document.getElementById("d2h-hide-hunks").onclick = () => {
      hunksHidden = !hunksHidden;
      document.getElementById("d2h-hide-hunks").textContent = hunksHidden
        ? "Show reviewed hunks"
        : "Hide reviewed hunks";
      document
        .querySelectorAll(".d2h-hunk-row, .d2h-hunk-header")
        .forEach((r) => {
          if (!r.classList.contains("d2h-hunk-reviewed")) return;
          r.classList.toggle("d2h-hunk-hidden", hunksHidden);
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
      const allHeaders = Array.from(
        document.querySelectorAll(".d2h-hunk-header"),
      );
      const seen = new Set();
      const first = allHeaders.find((h) => {
        if (seen.has(h.dataset.hunkId)) return false;
        seen.add(h.dataset.hunkId);
        return !h.classList.contains("d2h-hunk-reviewed");
      });
      if (first)
        first.scrollIntoView({ behavior: "smooth", block: "center" });
    };

    document.getElementById("d2h-clear-hunks").onclick = () => {
      if (!confirm("Clear all hunk review state?")) return;
      localStorage.removeItem(HUNK_KEY);
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
      .forEach(processFileForHunks);
    attachFileCheckboxes();
    attachEditorLinks();
    addToolbar();
    applyHunkState();
    restoreFileCheckboxes();
    updateProgress();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
