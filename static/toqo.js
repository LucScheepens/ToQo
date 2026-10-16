(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    return { ok: res.ok, status: res.status, data: await res.json().catch(() => ({})) };
  }

  // Confirmation prompts on destructive forms.
  document.addEventListener("submit", (e) => {
    const msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Copy-to-clipboard buttons.
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigator.clipboard?.writeText(btn.getAttribute("data-copy"));
      const old = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = old), 1500);
    });
  });

  // Format picker: show only the parameters relevant to the chosen format.
  const kindInputs = document.querySelectorAll('input[name="kind"]');
  function syncFormat() {
    const chosen = document.querySelector('input[name="kind"]:checked')?.value;
    document.querySelectorAll("[data-for]").forEach((el) => {
      const kinds = el.getAttribute("data-for").split(" ");
      el.classList.toggle("show", kinds.includes(chosen));
    });
  }
  kindInputs.forEach((i) => i.addEventListener("change", syncFormat));
  if (kindInputs.length) syncFormat();

  // Drag-and-drop schedule editing (SPEC §11.3).
  const moveUrl = document.body.querySelector("[data-move-url]")?.getAttribute("data-move-url");
  if (moveUrl) {
    let dragged = null;
    document.querySelectorAll(".card[draggable=true]").forEach((card) => {
      card.addEventListener("dragstart", (e) => {
        dragged = card;
        card.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", card.dataset.game);
      });
      card.addEventListener("dragend", () => card.classList.remove("dragging"));
    });
    document.querySelectorAll("[data-drop]").forEach((target) => {
      target.addEventListener("dragover", (e) => { e.preventDefault(); target.classList.add("drop-over"); });
      target.addEventListener("dragleave", () => target.classList.remove("drop-over"));
      target.addEventListener("drop", async (e) => {
        e.preventDefault();
        target.classList.remove("drop-over");
        const id = e.dataTransfer.getData("text/plain") || dragged?.dataset.game;
        if (!id) return;
        const body = target.dataset.unschedule
          ? { unschedule: true }
          : { space_id: target.dataset.space, start: target.dataset.start };
        const res = await postJSON(moveUrl.replace("/0/", "/" + id + "/"), body);
        if (!res.ok) alert(res.data.error || "Could not move the game.");
        location.reload();
      });
    });
  }

  // Result form: add/remove game columns for best-of matches.
  const scoreForm = document.querySelector(".score-form");
  if (scoreForm) {
    const table = scoreForm.querySelector(".score-table");
    const sync = () => {
      const heads = table.querySelectorAll("thead th.game-col");
      heads.forEach((th, i) => { th.firstChild.textContent = `Game ${i + 1}` + (i ? " " : ""); });
      scoreForm.classList.toggle("multi", heads.length > 1);
      scoreForm.querySelector(".add-game").hidden = heads.length >= 7;
    };
    scoreForm.addEventListener("click", (e) => {
      if (e.target.closest(".add-game")) {
        const th = document.createElement("th");
        th.className = "game-col";
        th.innerHTML = 'Game <button type="button" class="linklike remove-game" title="Remove this game">×</button>';
        table.querySelector("thead .add-col").before(th);
        let first;
        table.querySelectorAll("tbody tr").forEach((tr) => {
          const cols = tr.querySelectorAll("td.game-col");
          const td = cols[cols.length - 1].cloneNode(true);
          const input = td.querySelector("input");
          input.value = "";
          input.removeAttribute("autofocus");
          cols[cols.length - 1].after(td);
          first = first || input;
        });
        sync();
        first.focus();
      }
      const rm = e.target.closest(".remove-game");
      if (rm) {
        const idx = [...table.querySelectorAll("thead th.game-col")].indexOf(rm.closest("th"));
        rm.closest("th").remove();
        table.querySelectorAll("tbody tr").forEach((tr) => tr.querySelectorAll("td.game-col")[idx].remove());
        sync();
      }
    });
    sync();
  }

  // Queue board (SPEC §11.4): drag a pending game onto a free court, or tap the game and then
  // the court; drag a live game back to the list. Delegated, because live updates replace the markup.
  const moveForm = document.getElementById("queue-move");
  if (moveForm) {
    let drag = null;      // {id, live} while dragging
    let picked = null;    // selected pending card (tap-to-assign)
    const zone = (el) => el.closest?.("[data-court], [data-pending]");
    const accepts = (z, d) => d && (z.hasAttribute("data-court") ? !d.live : d.live);
    const submit = (action, game, space) => {
      moveForm.elements.action.value = action;
      moveForm.elements.game.value = game;
      moveForm.elements.space.value = space || "";
      document.body.classList.add("ui-busy");
      moveForm.submit();
    };
    const unpick = () => {
      picked?.classList.remove("selected");
      picked = null;
      document.body.classList.remove("picking");
    };

    document.addEventListener("dragstart", (e) => {
      const card = e.target.closest?.(".qcard[draggable=true]");
      if (!card) return;
      unpick();
      drag = { id: card.dataset.game, live: card.dataset.live === "1" };
      card.classList.add("dragging");
      document.body.classList.add("ui-busy");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", drag.id);
    });
    document.addEventListener("dragend", (e) => {
      e.target.closest?.(".qcard")?.classList.remove("dragging");
      document.querySelectorAll(".drop-over").forEach((z) => z.classList.remove("drop-over"));
      document.body.classList.remove("ui-busy");
      drag = null;
    });
    document.addEventListener("dragover", (e) => {
      const z = zone(e.target);
      if (!z || !accepts(z, drag)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      z.classList.add("drop-over");
    });
    document.addEventListener("dragleave", (e) => {
      const z = zone(e.target);
      if (z && !z.contains(e.relatedTarget)) z.classList.remove("drop-over");
    });
    document.addEventListener("drop", (e) => {
      const z = zone(e.target);
      if (!z || !accepts(z, drag)) return;
      e.preventDefault();
      submit(z.hasAttribute("data-court") ? "start" : "stop", drag.id, z.dataset.court);
    });

    // Tap/click (and Enter) to pick a pending game, then a free court.
    const activate = (target) => {
      const court = target.closest?.("[data-court]");
      if (court && picked) return submit("start", picked.dataset.game, court.dataset.court);
      const card = target.closest?.("[data-pending] .qcard[draggable=true]");
      if (!card) return;
      const same = card === picked;
      unpick();
      if (!same) {
        picked = card;
        card.classList.add("selected");
        document.body.classList.add("picking");
      }
    };
    document.addEventListener("click", (e) => {
      if (e.target.closest?.("[data-court], [data-pending] .qcard")) activate(e.target);
      else if (picked && !e.target.closest?.(".filters")) unpick();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") unpick();
      if (e.key === "Enter" && e.target.matches?.("[data-pending] .qcard")) activate(e.target);
    });

    // Filter pending games by poule/bracket (remembered for this tab).
    const applyFilter = () => {
      let f = "";
      try { f = sessionStorage.getItem("queue-filter") || ""; } catch (err) { /* storage blocked */ }
      if (f && !document.querySelector(`.filter[data-filter="${f}"]`)) f = "";
      document.querySelectorAll(".filter").forEach((b) => b.classList.toggle("on", b.dataset.filter === f));
      document.querySelectorAll("[data-pending] .qcard, .wait-row").forEach((el) =>
        el.classList.toggle("hidden", !!f && el.dataset.pool !== f));
    };
    document.addEventListener("click", (e) => {
      const b = e.target.closest?.(".filter");
      if (!b) return;
      try { sessionStorage.setItem("queue-filter", b.dataset.filter); } catch (err) { /* storage blocked */ }
      applyFilter();
    });
    document.addEventListener("live:updated", () => { unpick(); applyFilter(); });
    applyFilter();
  }

  // Live updates (SPEC §14): poll the version and swap <main> when it changes.
  const live = document.querySelector("[data-live-version]");
  if (live) {
    let version = live.getAttribute("data-live-version");
    const url = live.getAttribute("data-live-url");
    setInterval(async () => {
      if (document.hidden || document.body.classList.contains("ui-busy") ||
          document.body.classList.contains("picking")) return;
      const active = document.activeElement;
      if (active && live.contains(active) && /INPUT|SELECT|TEXTAREA/.test(active.tagName)) return;
      try {
        const res = await fetch(url, { credentials: "same-origin" });
        const data = await res.json();
        if (String(data.version) === String(version)) return;
        version = data.version;
        const html = await (await fetch(location.href, { credentials: "same-origin" })).text();
        const doc = new DOMParser().parseFromString(html, "text/html");
        const fresh = doc.querySelector("[data-live-version]");
        if (fresh) {
          live.innerHTML = fresh.innerHTML;
          live.setAttribute("data-live-version", version);
          document.dispatchEvent(new Event("live:updated"));
        }
      } catch (err) { /* offline: try again next tick */ }
    }, 15000);
  }
})();
