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

  // Live updates (SPEC §14): poll the version and swap <main> when it changes.
  const live = document.querySelector("[data-live-version]");
  if (live) {
    let version = live.getAttribute("data-live-version");
    const url = live.getAttribute("data-live-url");
    setInterval(async () => {
      if (document.hidden) return;
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
        }
      } catch (err) { /* offline: try again next tick */ }
    }, 15000);
  }
})();
