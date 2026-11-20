const API = "/api/v1";

const ALERT_LABELS = {
    NIGHT_PERSON: { title: "Человек в ночное время", cls: "alert-night", short: "Ночь" },
    YELLOW_VEST: { title: "Человек в жёлтом жилете", cls: "alert-vest", short: "Жилет" },
    UNKNOWN_PLATE: { title: "Проезд незнакомого ТС", cls: "alert-unknown", short: "Незнакомое ТС" },
    UNREAD_PLATE: { title: "ТС без распознанного номера", cls: "alert-unread", short: "Без номера" },
    VEHICLE_SEEN: { title: "Обнаружено ТС (сбор данных)", cls: "alert-seen", short: "ТС обнаружено" },
};

const DECISION_LABELS = {
    ALLOWED: { title: "свой", cls: "ok" },
    ALLOWED_FUZZY: { title: "свой ≈", cls: "fuzzy" },
    DENIED: { title: "чужой", cls: "warn" },
};

const PAGE_SIZES = [20, 50, 100, 200];

const state = {
    page: 0,
    size: 50,
    kind: "",
    cameraId: "",
    type: "",
    decision: "",
    q: "",
    from: "",
    to: "",
    auto: true,
};

const cameraNames = {};
let rows = [];
let headKey = null;      // ключ самой свежей записи — для определения «есть новые»
let rowsKey = null;      // подпись отрисованной выборки: не менялась — не перерисовываем tbody
let summaryTimer = null;
let inflight = null;
let lastSummary = null;

const $ = (id) => document.getElementById(id);

async function fetchJson(url, signal) {
    const resp = await fetch(url, { signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
}

function esc(s) {
    const div = document.createElement("div");
    div.textContent = s ?? "";
    return div.innerHTML;
}

function timeStr(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
}

function relativeTime(iso) {
    if (!iso) return "";
    const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    if (diff < 10) return "только что";
    if (diff < 60) return `${diff} с назад`;
    if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
    return `${Math.floor(diff / 86400)} дн назад`;
}

function entryKey(e) {
    return `${e.kind}:${e.id}:${e.detectedAt ?? ""}`;
}

// ---------------------------------------------------------------- параметры

/** Собирает query-строку из состояния + необязательных добавок. */
function buildQuery(extra = {}) {
    const p = new URLSearchParams();
    if (state.cameraId) p.set("cameraId", state.cameraId);
    if (state.kind) p.set("kind", state.kind);
    if (state.type) p.set("type", state.type);
    if (state.decision) p.set("decision", state.decision);
    if (state.q) p.set("q", state.q);
    if (state.from) p.set("from", state.from);
    if (state.to) p.set("to", state.to);
    for (const [key, value] of Object.entries(extra)) p.set(key, value);
    return p.toString();
}

/** Фильтры в адресной строке: ссылку на выбранный срез можно сохранить или отправить. */
function syncUrl() {
    const qs = buildQuery();
    history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

function readUrl() {
    const p = new URLSearchParams(location.search);
    for (const key of ["cameraId", "kind", "type", "decision", "q", "from", "to"]) {
        if (p.has(key)) state[key] = p.get(key);
    }
    if (p.has("size") && PAGE_SIZES.includes(Number(p.get("size")))) {
        state.size = Number(p.get("size"));
    }
}

function applyStateToControls() {
    $("q").value = state.q;
    $("from").value = state.from;
    $("to").value = state.to;
    $("size").value = String(state.size);
    $("camera-filter").value = state.cameraId;
    for (const tab of document.querySelectorAll("#kind-tabs .tab")) {
        tab.classList.toggle("active", tab.dataset.kind === state.kind);
    }
    syncKindVisibility();
    markActiveRange();
}

function syncKindVisibility() {
    $("type-filter").classList.toggle("hidden", state.kind !== "ALERT");
    $("decision-filter").classList.toggle("hidden", state.kind === "ALERT");
}

function markActiveRange() {
    const chips = [...document.querySelectorAll(".chip")];
    if (state.from || state.to) {
        chips.forEach(c => c.classList.remove("active"));
        return;
    }
    chips.forEach(c => c.classList.toggle("active", c.dataset.days === "0-all"));
}

function toLocalInput(date) {
    const pad = (n) => String(n).padStart(2, "0");
    // Секунды обязательны: без них 23:59 превращается в 23:59:00 и последняя
    // минута суток выпадает из выборки — у полей стоит step="1".
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
        + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// ---------------------------------------------------------------- журнал

async function loadJournal() {
    if (inflight) inflight.abort();
    const ctrl = new AbortController();
    inflight = ctrl;
    const url = `${API}/journal?${buildQuery({ page: state.page, size: state.size })}`;
    try {
        const data = await fetchJson(url, ctrl.signal);
        state.page = data.page;
        renderJournal(data);
    } catch (e) {
        if (e.name === "AbortError") return;
        renderError(e);
    } finally {
        if (inflight === ctrl) inflight = null;
    }
}

function renderError(e) {
    $("journal").innerHTML = `<tr><td colspan="8" class="empty error">Ошибка загрузки: ${esc(e.message)}</td></tr>`;
    $("pager-info").textContent = "—";
    $("pages").innerHTML = "";
    // tbody перезаписан строкой ошибки — прежняя подпись выборки больше недействительна.
    rowsKey = null;
}

function statusCell(entry) {
    if (entry.isPlate || entry.kind === "PLATE") {
        const d = DECISION_LABELS[entry.decision];
        return d ? `<span class="badge ${d.cls}">${d.title}</span>` : `<span class="muted">—</span>`;
    }
    const label = ALERT_LABELS[entry.alertType] || { title: entry.alertType, short: entry.alertType, cls: "" };
    return `<span class="badge badge-alert ${label.cls}">${esc(label.title)}</span>`;
}

function renderJournal(data) {
    rows = data.items ?? [];
    const tbody = $("journal");
    const sig = rowsSignature();

    // tbody.innerHTML пересоздаёт все <tr> вместе с <img>, и браузер заново
    // запрашивает каждую фотографию: таблица заметно мигает. Поэтому, если
    // выборка не изменилась, DOM не трогаем — правим только «5 с назад».
    if (sig !== rowsKey) {
        if (!rows.length) {
            tbody.innerHTML = `<tr><td colspan="8" class="empty">${state.q || state.cameraId || state.kind
                ? "По этим фильтрам ничего нет"
                : "Журнал пока пуст — события появятся после первого проезда"}</td></tr>`;
        } else {
            tbody.innerHTML = rows.map((e, i) => {
                const plate = e.plateNumber
                    ? `<span class="plate-number">${esc(e.plateNumber)}</span>`
                    : `<span class="muted">—</span>`;
                const kind = e.kind === "PLATE"
                    ? `<span class="kind kind-plate">номер</span>`
                    : `<span class="kind kind-alert">тревога</span>`;
                const photo = e.imagePath
                    ? `<img src="${esc(e.imagePath)}" loading="lazy" data-photo="${i}" alt="фото">`
                    : `<span class="muted">нет фото</span>`;
                return `
            <tr>
                <td class="td-time" title="${esc(relativeTime(e.detectedAt))}">
                    ${timeStr(e.detectedAt)}<span class="muted block">${esc(relativeTime(e.detectedAt))}</span>
                </td>
                <td>${kind}</td>
                <td>${plate}</td>
                <td>${statusCell(e)}</td>
                <td>${photo}</td>
                <td>${esc(cameraNames[e.cameraId] || e.cameraId)}</td>
                <td class="td-num">${e.confidence != null ? (e.confidence * 100).toFixed(1) + "%" : "—"}</td>
                <td class="td-num">${e.trackId != null ? "#" + esc(e.trackId) : "—"}</td>
            </tr>`;
            }).join("");
        }
        rowsKey = sig;
    } else {
        refreshRelativeTimes();
    }

    if (rows.length) headKey = entryKey(rows[0]);
    renderPager(data);
    $("stat-last").textContent = new Date().toLocaleTimeString("ru-RU");
}

/** Подпись выборки: фильтры + ключи и фотографии строк. */
function rowsSignature() {
    const filters = [state.q, state.cameraId, state.kind, state.type, state.decision].join("~");
    return filters + "##" + rows.map(e => `${entryKey(e)}|${e.imagePath ?? ""}`).join(";");
}

/** Обновляет подписи «только что / 5 с назад» на месте, не трогая остальной DOM. */
function refreshRelativeTimes() {
    const cells = $("journal").querySelectorAll("td.td-time");
    cells.forEach((cell, i) => {
        const iso = rows[i]?.detectedAt;
        if (!iso) return;
        const rel = relativeTime(iso);
        cell.title = rel;
        const span = cell.querySelector(".muted.block");
        if (span) span.textContent = rel;
    });
}

function renderPager(data) {
    const total = data.total ?? 0;
    const size = data.size ?? state.size;
    const page = data.page ?? 0;
    const totalPages = data.totalPages ?? 0;
    state.size = size;
    state.page = page;

    const from = total === 0 ? 0 : page * size + 1;
    const to = Math.min(total, (page + 1) * size);
    $("pager-info").textContent = total === 0
        ? "Нет записей"
        : `Показано ${from}–${to} из ${total} · страница ${page + 1} из ${Math.max(totalPages, 1)}`;
    $("size").value = String(size);
    $("pages").innerHTML = renderPages(page, totalPages);
}

/** Номера страниц с многоточиями: 1 … 4 5 [6] 7 8 … 20 */
function renderPages(current, totalPages) {
    if (totalPages <= 1) return "";
    const parts = [];
    const add = (p) => parts.push(
        `<button class="page${p === current ? " active" : ""}" data-page="${p}">${p + 1}</button>`);
    const gap = () => parts.push(`<span class="page-gap">…</span>`);

    add(0);
    if (current > 3) gap();
    for (let p = Math.max(1, current - 1); p <= Math.min(totalPages - 2, current + 1); p++) add(p);
    if (current < totalPages - 4) gap();
    if (totalPages > 1) add(totalPages - 1);
    return parts.join("");
}

// ---------------------------------------------------------------- сводка

async function loadSummary() {
    const url = `${API}/stats/summary?${buildQuery({ days: 14 })}`;
    try {
        const s = await fetchJson(url);
        lastSummary = s;
        $("stat-plates").textContent = s.platesTotal ?? 0;
        $("stat-unique").textContent = s.platesUnique ?? 0;
        $("stat-denied").textContent = s.platesDenied ?? 0;
        $("stat-alerts").textContent = s.alertsTotal ?? 0;
        renderAlertTypes(s.alertsByType || {});
        renderRangeLabel();
    } catch (e) {
        console.error("Сводка не загрузилась:", e);
    }
}

function renderAlertTypes(byType) {
    const select = $("type-filter");
    const current = state.type;
    const known = Object.keys(ALERT_LABELS);
    const present = known.filter(k => k in byType);
    const extra = Object.keys(byType).filter(k => !known.includes(k));
    const values = [...present, ...extra];

    select.innerHTML = `<option value="">Все типы тревог (${Object.values(byType).reduce((a, b) => a + b, 0)})</option>`;
    for (const key of values) {
        const opt = document.createElement("option");
        opt.value = key;
        opt.textContent = `${ALERT_LABELS[key]?.title ?? key} — ${byType[key]}`;
        select.appendChild(opt);
    }
    select.value = current;
    if (current && !values.includes(current)) {
        // Типа нет в текущем срезе, но он остался в фильтре — не молча теряем его
        const opt = document.createElement("option");
        opt.value = current;
        opt.textContent = `${ALERT_LABELS[current]?.title ?? current} — 0`;
        select.appendChild(opt);
        select.value = current;
    }
}

function renderRangeLabel() {
    const label = $("range-label");
    if (state.from || state.to) {
        label.textContent = `с ${state.from ? timeStr(state.from) : "…"} по ${state.to ? timeStr(state.to) : "…"}`;
    } else {
        label.textContent = "за всё время";
    }
}

// ---------------------------------------------------------------- обновление

/** Тихая перерисовка: не трогает страницу, если пользователь смотрит не первую. */
async function refresh() {
    if (state.page > 0) {
        await checkForNew();
        return;
    }
    await Promise.all([loadJournal(), loadSummary()]);
}

/** Пока пользователь на другой странице, достаточно узнать, изменилась ли «голова» ленты. */
async function checkForNew() {
    try {
        const data = await fetchJson(`${API}/journal?${buildQuery({ page: 0, size: 1 })}`);
        const first = (data.items ?? [])[0];
        if (first && entryKey(first) !== headKey) {
            $("new-entries").classList.remove("hidden");
        }
    } catch (e) {
        /* фоновая проверка: молча игнорируем */
    }
}

function startAuto() {
    if (summaryTimer) clearInterval(summaryTimer);
    if (state.auto) {
        summaryTimer = setInterval(refresh, 5000);
    }
}

// ---------------------------------------------------------------- модальное окно

function openModal(index) {
    const entry = rows[index];
    if (!entry?.imagePath) return;
    hideModalNoPhoto();
    $("modal-img").src = entry.imagePath;
    $("modal-caption").innerHTML = entry.plateNumber
        ? `<b>${esc(entry.plateNumber)}</b> · ${statusCell(entry).replace(/<[^>]+>/g, "")} · ${timeStr(entry.detectedAt)}`
        : `${esc(ALERT_LABELS[entry.alertType]?.title ?? entry.alertType)} · ${timeStr(entry.detectedAt)}`;
    $("modal").classList.remove("hidden");
    $("modal").dataset.index = String(index);
    updateModalNav();
}

function moveModal(delta) {
    const current = Number($("modal").dataset.index || -1);
    const photos = rows.map((e, i) => (e.imagePath ? i : -1)).filter(i => i >= 0);
    const pos = photos.indexOf(current);
    const next = photos[pos + delta];
    if (next === undefined) return;
    openModal(next);
}

function updateModalNav() {
    const photos = rows.filter(e => e.imagePath).length;
    const multi = photos > 1;
    $("modal-prev").classList.toggle("hidden", !multi);
    $("modal-next").classList.toggle("hidden", !multi);
}

function closeModal() {
    $("modal").classList.add("hidden");
    hideModalNoPhoto();
    $("modal-img").src = "";
}

function showModalNoPhoto() {
    const img = $("modal-img");
    const modal = $("modal");
    img.classList.add("hidden");
    let msg = modal.querySelector(".modal-no-photo");
    if (!msg) {
        msg = Object.assign(document.createElement("div"), {
            className: "modal-no-photo",
            textContent: "Фото недоступно",
        });
        modal.appendChild(msg);
    }
    msg.classList.remove("hidden");
}

function hideModalNoPhoto() {
    const img = $("modal-img");
    img.classList.remove("hidden");
    const msg = $("modal").querySelector(".modal-no-photo");
    if (msg) msg.classList.add("hidden");
}

// ---------------------------------------------------------------- события UI

async function loadCameras() {
    try {
        const list = await fetchJson(`${API}/cameras`);
        const select = $("camera-filter");
        select.innerHTML = `<option value="">Все камеры</option>`;
        for (const cam of list) {
            cameraNames[cam.id] = cam.name || cam.id;
            const opt = document.createElement("option");
            opt.value = cam.id;
            opt.textContent = cam.name || cam.id;
            select.appendChild(opt);
        }
        select.value = state.cameraId;
    } catch (e) {
        console.error("Не удалось загрузить камеры:", e);
    }
}

let searchTimer = null;

function onFilterChanged({ resetPage = true } = {}) {
    if (resetPage) state.page = 0;
    $("new-entries").classList.add("hidden");
    syncUrl();
    renderRangeLabel();
    loadJournal();
    loadSummary();
}

function debounceSearch() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        state.q = $("q").value.trim();
        onFilterChanged();
    }, 300);
}

function bindEvents() {
    $("q").addEventListener("input", debounceSearch);

    $("camera-filter").addEventListener("change", () => {
        state.cameraId = $("camera-filter").value;
        onFilterChanged();
    });

    for (const tab of document.querySelectorAll("#kind-tabs .tab")) {
        tab.addEventListener("click", () => {
            state.kind = tab.dataset.kind;
            for (const t of document.querySelectorAll("#kind-tabs .tab")) {
                t.classList.toggle("active", t === tab);
            }
            // Статус номера и тип тревоги несовместимы: сбрасываем «чужие» фильтры,
            // иначе вкладка «Тревоги» покажет пустую таблицу из-за decision=ALLOWED.
            if (state.kind === "ALERT") state.decision = "";
            if (state.kind !== "ALERT") state.type = "";
            $("type-filter").value = state.type;
            $("decision-filter").value = state.decision;
            syncKindVisibility();
            onFilterChanged();
        });
    }

    $("type-filter").addEventListener("change", () => {
        state.type = $("type-filter").value;
        onFilterChanged();
    });

    $("decision-filter").addEventListener("change", () => {
        state.decision = $("decision-filter").value;
        onFilterChanged();
    });

    for (const chip of document.querySelectorAll(".chip")) {
        chip.addEventListener("click", () => {
            const days = chip.dataset.days;
            if (days === "0-all") {
                state.from = "";
                state.to = "";
            } else {
                // Период — целые сутки: с 00:00 первого дня до 23:59:59 последнего.
                // Раньше верхняя граница была «сейчас», а для «Сегодня» (days=0) нижняя
                // уезжала на завтра — from.setDate(getDate() - (0 - 1)), — диапазон
                // выворачивался наружу и выборка всегда была пустой.
                const n = Math.max(Number(days), 1);
                const from = new Date();
                from.setDate(from.getDate() - (n - 1));
                from.setHours(0, 0, 0, 0);
                const to = new Date(from);
                to.setDate(to.getDate() + (n - 1));
                to.setHours(23, 59, 59, 999);
                state.from = toLocalInput(from);
                state.to = toLocalInput(to);
            }
            $("from").value = state.from;
            $("to").value = state.to;
            for (const c of document.querySelectorAll(".chip")) c.classList.toggle("active", c === chip);
            onFilterChanged();
        });
    }

    $("from").addEventListener("change", () => {
        state.from = $("from").value;
        markActiveRange();
        onFilterChanged();
    });

    $("to").addEventListener("change", () => {
        state.to = $("to").value;
        markActiveRange();
        onFilterChanged();
    });

    $("reset").addEventListener("click", () => {
        Object.assign(state, { page: 0, kind: "", cameraId: "", type: "", decision: "", q: "", from: "", to: "" });
        for (const tab of document.querySelectorAll("#kind-tabs .tab")) {
            tab.classList.toggle("active", tab.dataset.kind === "");
        }
        $("q").value = "";
        $("from").value = "";
        $("to").value = "";
        $("camera-filter").value = "";
        $("type-filter").value = "";
        $("decision-filter").value = "";
        syncKindVisibility();
        for (const c of document.querySelectorAll(".chip")) c.classList.toggle("active", c.dataset.days === "0-all");
        onFilterChanged();
    });

    $("size").addEventListener("change", () => {
        state.size = Number($("size").value);
        state.page = 0;
        loadJournal();
    });

    $("auto").addEventListener("change", () => {
        state.auto = $("auto").checked;
        startAuto();
    });

    $("export").addEventListener("click", () => {
        const p = new URLSearchParams(buildQuery());
        p.delete("page");
        p.delete("size");
        location.href = `${API}/journal/export.csv?${p.toString()}`;
    });

    $("pages").addEventListener("click", (e) => {
        const btn = e.target.closest("button.page");
        if (!btn) return;
        state.page = Number(btn.dataset.page);
        loadJournal();
        window.scrollTo({ top: 0, behavior: "smooth" });
    });

    $("new-entries-btn").addEventListener("click", () => {
        state.page = 0;
        $("new-entries").classList.add("hidden");
        loadJournal();
    });

    $("journal").addEventListener("click", (e) => {
        const img = e.target.closest("img[data-photo]");
        if (img) openModal(Number(img.dataset.photo));
    });

    // Фото может отсутствовать: старые записи, другой сервер, неверный
    // argus.images-dir. Событие error не всплывает, поэтому ловим в фазе
    // захвата, иначе в таблице останется иконка «сломанный файл».
    const markBroken = (e) => {
        const img = e.target;
        if (!(img instanceof HTMLImageElement) || !img.dataset.photo) return;
        const span = document.createElement("span");
        span.className = "muted no-photo";
        span.textContent = "фото недоступно";
        img.replaceWith(span);
    };
    $("journal").addEventListener("error", markBroken, true);
    $("modal-img").addEventListener("error", showModalNoPhoto);
    $("modal-img").addEventListener("load", hideModalNoPhoto);
    $("modal-close").addEventListener("click", closeModal);
    $("modal-prev").addEventListener("click", (e) => { e.stopPropagation(); moveModal(-1); });
    $("modal-next").addEventListener("click", (e) => { e.stopPropagation(); moveModal(1); });
    $("modal").addEventListener("click", (e) => {
        if (e.target === $("modal") || e.target === $("modal-img")) closeModal();
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            if (!$("modal").classList.contains("hidden")) closeModal();
            return;
        }
        if (!$("modal").classList.contains("hidden")) {
            if (e.key === "ArrowLeft") moveModal(-1);
            if (e.key === "ArrowRight") moveModal(1);
            return;
        }
        if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "SELECT") {
            e.preventDefault();
            $("q").focus();
        }
    });
}

(async () => {
    readUrl();
    applyStateToControls();
    bindEvents();
    await loadCameras();
    await Promise.all([loadJournal(), loadSummary()]);
    startAuto();
    void lastSummary;
})();
