// VoyageAI frontend.
// The conversation id lives in an httpOnly cookie set by the server,
// so this file never stores or sends a thread id.

let latestPlanMarkdown = "";   // last answer that was a full plan (for Copy / PDF)
let busy = false;

function $(id) { return document.getElementById(id); }

function setPrompt(text) {
    $("userInput").value = text;
    $("userInput").focus();
}

function setLoading(isLoading) {
    busy = isLoading;
    $("sendBtn").disabled = isLoading;
    $("btnText").classList.toggle("hidden", isLoading);
    $("btnLoader").classList.toggle("hidden", !isLoading);
}

function showError(message) {
    $("errorBox").textContent = message;
    $("errorBox").classList.remove("hidden");
}

function hideError() {
    $("errorBox").classList.add("hidden");
    $("errorBox").textContent = "";
}

// Markdown -> HTML, then sanitize. The answer contains text from web pages,
// so it must never be inserted as raw HTML.
function renderMarkdown(markdown) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
        const pre = document.createElement("div");
        pre.innerText = markdown;            // safe fallback: plain text
        return pre.innerHTML;
    }
    const html = DOMPurify.sanitize(marked.parse(markdown));
    return html;
}

function openLinksInNewTab(container) {
    container.querySelectorAll("a[href]").forEach(a => {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
    });
}

function isPlan(markdown) {
    return markdown.includes("## Flights") && markdown.includes("## Budget");
}

function addMessage(role, content, scroll = true) {
    $("chatSection").classList.remove("hidden");
    $("inputTitle").textContent = "Reply to a question or change the plan, e.g. \"make it nonstop only\".";
    $("quickPrompts").classList.add("hidden");

    const row = document.createElement("div");
    row.className = `chat-row ${role}`;
    const bubble = document.createElement("div");

    if (role === "user") {
        bubble.className = "user-bubble";
        bubble.textContent = content;
    } else {
        bubble.className = "result-box assistant-bubble";
        bubble.innerHTML = renderMarkdown(content);
        openLinksInNewTab(bubble);
        if (isPlan(content)) latestPlanMarkdown = content;
    }
    row.appendChild(bubble);
    $("chatLog").appendChild(row);
    if (scroll) row.scrollIntoView({ behavior: "smooth", block: "start" });
}

function addPending() {
    const row = document.createElement("div");
    row.className = "chat-row assistant";
    row.id = "pendingRow";
    row.innerHTML = '<div class="pending-bubble"><span class="loader"></span> Searching flights, hotels and things to do…</div>';
    $("chatLog").appendChild(row);
}

function removePending() {
    const row = $("pendingRow");
    if (row) row.remove();
}

async function sendMessage() {
    if (busy) return;
    hideError();
    const message = $("userInput").value.trim();
    if (!message) {
        showError("Please type your travel request first.");
        return;
    }

    addMessage("user", message);
    $("userInput").value = "";
    addPending();
    setLoading(true);

    try {
        const response = await fetch("/api/travel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ message })
        });
        let data;
        try { data = await response.json(); } catch { data = {}; }
        if (!response.ok || !data.success) {
            throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
        }
        removePending();
        addMessage("assistant", data.answer);
    } catch (error) {
        removePending();
        showError(error.message);
        $("userInput").value = message;   // let the user retry without retyping
    } finally {
        setLoading(false);
    }
}

async function newTrip() {
    await fetch("/api/new-trip", { method: "POST", credentials: "same-origin" });
    $("chatLog").innerHTML = "";
    $("chatSection").classList.add("hidden");
    $("quickPrompts").classList.remove("hidden");
    $("inputTitle").textContent = "Describe your trip in your own words. I'll ask about anything missing.";
    latestPlanMarkdown = "";
    hideError();
    $("userInput").focus();
}

async function loadHistory() {
    try {
        const response = await fetch("/api/history", { credentials: "same-origin" });
        const data = await response.json();
        (data.messages || []).forEach(m => addMessage(m.role, m.content, false));
    } catch {
        // No history is fine; start fresh.
    }
}

function copyResult() {
    if (!latestPlanMarkdown) {
        showError("No full plan yet to copy.");
        return;
    }
    navigator.clipboard.writeText(latestPlanMarkdown)
        .then(() => {
            const btn = document.querySelector(".copy-btn");
            const old = btn.textContent;
            btn.textContent = "Copied!";
            setTimeout(() => { btn.textContent = old; }, 1400);
        })
        .catch(() => showError("Could not copy the plan."));
}

function downloadPDF() {
    if (!latestPlanMarkdown) {
        showError("No full plan yet to download.");
        return;
    }
    $("pdfBox").innerHTML = renderMarkdown(latestPlanMarkdown);
    const btn = document.querySelector(".download-btn");
    const old = btn.textContent;
    btn.textContent = "Preparing PDF...";
    btn.disabled = true;

    const container = $("pdfContent");
    // html2canvas captures relative to the current scroll position: if the page is
    // scrolled down, the PDF starts with blank space and the end is cut off.
    const savedScroll = window.scrollY;
    window.scrollTo(0, 0);
    container.classList.remove("pdf-offscreen");
    html2pdf()
        .set({
            margin: 0.5,
            filename: "voyageai-travel-plan.pdf",
            image: { type: "jpeg", quality: 0.98 },
            html2canvas: { scale: 2, useCORS: true, backgroundColor: "#ffffff", scrollX: 0, scrollY: 0 },
            jsPDF: { unit: "in", format: "a4", orientation: "portrait" },
            pagebreak: { mode: ["css", "legacy"] }
        })
        .from(container)
        .save()
        .catch(() => showError("Could not create the PDF."))
        .finally(() => {
            container.classList.add("pdf-offscreen");
            setTimeout(() => window.scrollTo(0, savedScroll), 50);   // after html2pdf cleans up
            btn.textContent = old;
            btn.disabled = false;
        });
}

document.addEventListener("keydown", event => {
    if (event.ctrlKey && event.key === "Enter" && !$("chatPanel").classList.contains("hidden")) sendMessage();
});

// =====================================================================
// Tabs
// =====================================================================

function showTab(name) {
    const isForm = name === "form";
    $("formPanel").classList.toggle("hidden", !isForm);
    $("chatPanel").classList.toggle("hidden", isForm);
    $("tabForm").classList.toggle("active", isForm);
    $("tabChat").classList.toggle("active", !isForm);
    $("tabForm").setAttribute("aria-selected", String(isForm));
    $("tabChat").setAttribute("aria-selected", String(!isForm));
    try { localStorage.setItem("voyage_tab", name); } catch { /* storage may be blocked */ }
}

// =====================================================================
// Airport autocomplete (From / To)
// The chosen suggestion is stored on the input as data-city / data-state,
// which is exactly what the backend's airport resolver needs.
// =====================================================================

function setupAutocomplete(input, list) {
    let timer = null;
    let items = [];
    let active = -1;

    function close() { list.classList.add("hidden"); active = -1; }

    function choose(item) {
        input.value = item.label;
        input.dataset.city = item.city;
        input.dataset.state = item.state || "";
        input.classList.remove("invalid");
        close();
    }

    function render() {
        list.innerHTML = "";
        items.forEach((item, i) => {
            const option = document.createElement("div");
            option.className = "ac-item" + (i === active ? " active" : "");
            option.setAttribute("role", "option");
            option.textContent = item.label;                       // textContent: safe
            option.addEventListener("mousedown", e => { e.preventDefault(); choose(item); });
            list.appendChild(option);
        });
        list.classList.toggle("hidden", items.length === 0);
    }

    input.addEventListener("input", () => {
        delete input.dataset.city;                                  // typed text, not a picked item
        delete input.dataset.state;
        clearTimeout(timer);
        const q = input.value.trim();
        if (q.length < 2) { items = []; close(); return; }
        timer = setTimeout(async () => {
            try {
                const r = await fetch(`/api/airports?q=${encodeURIComponent(q)}`);
                items = (await r.json()).results || [];
                active = -1;
                render();
            } catch { close(); }
        }, 200);
    });

    input.addEventListener("keydown", e => {
        if (list.classList.contains("hidden")) return;
        if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); render(); e.preventDefault(); }
        else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
        else if (e.key === "Enter" && active >= 0) { choose(items[active]); e.preventDefault(); }
        else if (e.key === "Escape") close();
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
}

function swapRoute() {
    const a = $("fromInput"), b = $("toInput");
    [a.value, b.value] = [b.value, a.value];
    const ad = { ...a.dataset }, bd = { ...b.dataset };
    ["city", "state"].forEach(k => {
        if (bd[k] !== undefined) a.dataset[k] = bd[k]; else delete a.dataset[k];
        if (ad[k] !== undefined) b.dataset[k] = ad[k]; else delete b.dataset[k];
    });
}

// =====================================================================
// Form controls
// =====================================================================

let tripType = "round";

function isoDate(d) {
    const z = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}

function setupDates() {
    const today = new Date();
    const max = new Date(); max.setDate(max.getDate() + 330);        // flight search limit (~11 months)
    const depart = $("departDate"), ret = $("returnDate");
    depart.min = isoDate(today); depart.max = isoDate(max);
    ret.max = isoDate(max);
    depart.addEventListener("change", () => {
        if (!depart.value) return;
        const next = new Date(depart.value + "T00:00:00"); next.setDate(next.getDate() + 1);
        ret.min = isoDate(next);
        if (!ret.value || ret.value <= depart.value) {
            const plus3 = new Date(depart.value + "T00:00:00"); plus3.setDate(plus3.getDate() + 3);
            ret.value = isoDate(plus3);
        }
    });
}

function setTripType(type) {
    tripType = type;
    document.querySelectorAll(".seg").forEach(b => b.classList.toggle("active", b.dataset.trip === type));
    $("returnField").classList.toggle("hidden", type === "oneway");
    $("nightsField").classList.toggle("hidden", type !== "oneway");
}

function step(id, delta) {
    const el = $(id);
    const box = el.parentElement;
    const min = Number(box.dataset.min), max = Number(box.dataset.max);
    el.textContent = String(Math.min(max, Math.max(min, Number(el.textContent) + delta)));
}

function setupChips() {
    document.querySelectorAll("#interestChips .chip").forEach(chip =>
        chip.addEventListener("click", () => chip.classList.toggle("selected")));
}

function placeFromInput(input) {
    // A picked suggestion gives exact city + state; otherwise send what was typed
    // and let the server resolve it (it will ask if the city is ambiguous).
    if (input.dataset.city) return { city: input.dataset.city, state: input.dataset.state || null };
    return { city: input.value.trim(), state: null };
}

function readForm() {
    const errors = [];
    const from = placeFromInput($("fromInput")), to = placeFromInput($("toInput"));
    if (from.city.length < 2) { errors.push("Choose where you're flying from."); $("fromInput").classList.add("invalid"); }
    if (to.city.length < 2) { errors.push("Choose where you're going."); $("toInput").classList.add("invalid"); }

    const depart = $("departDate").value, ret = $("returnDate").value;
    if (!depart) errors.push("Pick a departure date.");
    if (tripType === "round") {
        if (!ret) errors.push("Pick a return date.");
        else if (depart && ret <= depart) errors.push("The return date must be after the departure date.");
    }
    const nights = Number($("nightsInput").value);
    if (tripType === "oneway" && (!Number.isInteger(nights) || nights < 0 || nights > 30)) {
        errors.push("Hotel nights must be between 0 and 30.");
    }
    const budget = $("budgetInput").value ? Number($("budgetInput").value) : null;
    if (budget !== null && !(budget > 0)) errors.push("The budget must be a positive number.");

    const form = {
        origin_city: from.city, origin_state: from.state,
        destination_city: to.city, destination_state: to.state,
        departure_date: depart,
        return_date: tripType === "round" ? ret : null,
        one_way: tripType === "oneway",
        nights: tripType === "oneway" ? nights : null,
        adults: Number($("adults").textContent),
        children: Number($("children").textContent),
        budget_usd: budget,
        nonstop_only: $("nonstopInput").checked,
        travel_class: $("cabinSelect").value,
        min_hotel_rating: $("ratingSelect").value ? Number($("ratingSelect").value) : null,
        allow_rentals: $("rentalsInput").checked,
        allow_hostels: $("hostelsInput").checked,
        interests: [...document.querySelectorAll("#interestChips .chip.selected")].map(c => c.textContent.trim()),
    };
    return { form, errors };
}

function formSummary(f) {
    const short = s => s.replace(/\s*\(.*\)$/, "");                  // drop "(all airports...)"
    const parts = [`${short($("fromInput").value)} → ${short($("toInput").value)}`,
                   f.one_way ? `${f.departure_date} (one way)` : `${f.departure_date} to ${f.return_date}`,
                   `${f.adults} adult(s)` + (f.children ? `, ${f.children} child(ren)` : "")];
    if (f.budget_usd) parts.push(`budget $${f.budget_usd.toLocaleString()}`);
    if (f.nonstop_only) parts.push("nonstop only");
    if (f.interests.length) parts.push("interests: " + f.interests.join(", "));
    return "📝 Trip form: " + parts.join(" · ");
}

async function submitForm(event) {
    event.preventDefault();
    if (busy) return;
    hideError();
    document.querySelectorAll(".invalid").forEach(el => el.classList.remove("invalid"));
    const { form, errors } = readForm();
    if (errors.length) { showError(errors.join(" ")); return; }

    addMessage("user", formSummary(form));
    addPending();
    busy = true;
    const btn = $("formBtn");
    btn.disabled = true;
    btn.querySelector(".btn-label").textContent = "Searching…";
    btn.querySelector(".loader").classList.remove("hidden");
    try {
        const response = await fetch("/api/plan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify(form)
        });
        let data;
        try { data = await response.json(); } catch { data = {}; }
        if (!response.ok || !data.success) {
            const detail = Array.isArray(data.detail) ? "Please check the form fields." : null;
            throw new Error(data.error || detail || `Request failed (HTTP ${response.status}).`);
        }
        removePending();
        addMessage("assistant", data.answer);
    } catch (error) {
        removePending();
        showError(error.message);
    } finally {
        busy = false;
        btn.disabled = false;
        btn.querySelector(".btn-label").textContent = "Plan my trip";
        btn.querySelector(".loader").classList.add("hidden");
    }
}

// =====================================================================
// Start-up
// =====================================================================

setupAutocomplete($("fromInput"), $("fromList"));
setupAutocomplete($("toInput"), $("toList"));
setupDates();
setupChips();
try { if (localStorage.getItem("voyage_tab") === "chat") showTab("chat"); } catch { /* ignore */ }

loadHistory();
