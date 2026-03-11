const WEEKDAY_LABELS = ["", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"];
const WEEKDAY_SHORT = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const SCOPE_LABELS = {
  all_day: "全天",
  morning: "上午",
  afternoon: "下午",
  evening: "晚上",
};
const SCOPE_PERIODS = {
  all_day: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
  morning: [1, 2, 3, 4, 5],
  afternoon: [6, 7, 8, 9],
  evening: [10, 11, 12],
};
const WEEKDAY_ALIAS = {
  一: 1,
  二: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  日: 7,
  天: 7,
};

const state = {
  dataset: null,
  selectedPeople: [],
  week: 1,
  weekday: 1,
  scope: "all_day",
  date: "",
  lastKnownToday: "",
  themePreference: "auto",
};

const refs = {};

document.addEventListener("DOMContentLoaded", async () => {
  bindRefs();
  bindBaseEvents();
  startLiveUi();
  await loadDataset();
});

function bindRefs() {
  refs.heroSummary = document.getElementById("hero-summary");
  refs.liveClock = document.getElementById("live-clock");
  refs.liveDate = document.getElementById("live-date");
  refs.themeStatus = document.getElementById("theme-status");
  refs.themeToggles = Array.from(document.querySelectorAll("[data-theme-mode]"));
  refs.datasetMeta = document.getElementById("dataset-meta");
  refs.queryDate = document.getElementById("query-date");
  refs.queryWeek = document.getElementById("query-week");
  refs.queryScope = document.getElementById("query-scope");
  refs.weekdayPills = document.getElementById("weekday-pills");
  refs.peopleFilters = document.getElementById("people-filters");
  refs.peopleCount = document.getElementById("people-count");
  refs.matrixMeta = document.getElementById("matrix-meta");
  refs.summaryStrip = document.getElementById("summary-strip");
  refs.matrixHead = document.getElementById("matrix-head");
  refs.matrixBody = document.getElementById("matrix-body");
  refs.heatmapRoot = document.getElementById("heatmap-root");
  refs.rankingsRoot = document.getElementById("rankings-root");
  refs.nlForm = document.getElementById("nl-form");
  refs.nlQuestion = document.getElementById("nl-question");
  refs.nlResult = document.getElementById("nl-result");
  refs.detailModal = document.getElementById("detail-modal");
  refs.detailTitle = document.getElementById("detail-title");
  refs.detailContent = document.getElementById("detail-content");
}

function bindBaseEvents() {
  refs.queryDate.addEventListener("change", () => {
    const parsed = fromIsoDate(refs.queryDate.value);
    if (!parsed) return;
    state.date = refs.queryDate.value;
    state.week = clampWeek(dateToWeek(parsed), 1);
    state.weekday = parsed.getDay() === 0 ? 7 : parsed.getDay();
    syncQueryInputs();
    render();
  });

  refs.queryWeek.addEventListener("input", () => {
    state.week = clampWeek(Number(refs.queryWeek.value) || 1, 1);
    state.date = toIsoDate(deriveDate(state.week, state.weekday));
    syncQueryInputs();
    render();
  });

  refs.queryScope.addEventListener("change", () => {
    state.scope = refs.queryScope.value;
    render();
  });

  refs.themeToggles.forEach((button) => {
    button.addEventListener("click", () => {
      setThemePreference(button.dataset.themeMode || "auto");
    });
  });

  document.getElementById("people-select-all").addEventListener("click", () => {
    state.selectedPeople = allPeopleNames();
    syncPeopleChecks();
    render();
  });

  document.getElementById("people-clear").addEventListener("click", () => {
    state.selectedPeople = [];
    syncPeopleChecks();
    render();
  });

  document.getElementById("people-only-free").addEventListener("click", () => {
    const snapshot = buildSnapshot(state.scope, state.week, state.weekday, allPeopleNames());
    state.selectedPeople = snapshot.summary.groups.all_free.slice();
    syncPeopleChecks();
    render();
  });

  document.getElementById("jump-today").addEventListener("click", () => jumpRelative(0));
  document.getElementById("jump-tomorrow").addEventListener("click", () => jumpRelative(1));
  document.getElementById("jump-current-week").addEventListener("click", () => {
    if (!state.dataset) return;
    const today = getBrowserToday();
    state.week = clampWeek(dateToWeek(today), 1);
    state.weekday = today.getDay() === 0 ? 7 : today.getDay();
    state.date = toIsoDate(today);
    syncQueryInputs();
    render();
  });

  refs.nlForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = refs.nlQuestion.value.trim();
    if (!question) {
      refs.nlResult.innerHTML = `<div class="empty-state">请输入查询内容。</div>`;
      return;
    }
    const parsed = parseNaturalQuestion(question);
    if (!parsed.week || !parsed.weekday) {
      refs.nlResult.innerHTML = `<div class="empty-state">没能完整识别日期，请改成“第3周周五下午”或“4月2日下午”这类表达。</div>`;
      return;
    }
    if (parsed.people_names.length > 0) {
      state.selectedPeople = parsed.people_names.slice();
      syncPeopleChecks();
    }
    state.week = clampWeek(parsed.week, 1);
    state.weekday = parsed.weekday;
    state.scope = parsed.scope || "all_day";
    state.date = parsed.date || toIsoDate(deriveDate(state.week, state.weekday));
    syncQueryInputs();
    render();
    renderNaturalResult(question);
  });

  document.getElementById("detail-close").addEventListener("click", closeDetail);
  refs.detailModal.addEventListener("click", (event) => {
    if (event.target === refs.detailModal) {
      closeDetail();
    }
  });
}

function startLiveUi() {
  state.themePreference = loadThemePreference();
  updateLiveUi();
  window.setInterval(updateLiveUi, 1000);
}

function updateLiveUi() {
  const now = new Date();
  const today = getBrowserToday();
  const todayIso = toIsoDate(today);
  if (refs.liveClock) {
    refs.liveClock.textContent = formatClock(now);
  }
  if (refs.liveDate) {
    refs.liveDate.textContent = `${todayIso} · ${WEEKDAY_LABELS[today.getDay() === 0 ? 7 : today.getDay()]}`;
  }
  applyTheme(now);
  const previousToday = state.lastKnownToday;
  state.lastKnownToday = todayIso;
  if (state.dataset && previousToday && previousToday !== todayIso && state.date === previousToday) {
    state.week = clampWeek(dateToWeek(today), 1);
    state.weekday = today.getDay() === 0 ? 7 : today.getDay();
    state.date = todayIso;
    syncQueryInputs();
    render();
  }
}

function applyTheme(now) {
  const theme = state.themePreference === "auto" ? resolveThemeMode(now) : state.themePreference;
  document.documentElement.dataset.theme = theme;
  if (refs.themeStatus) {
    refs.themeStatus.dataset.theme = theme;
    refs.themeStatus.textContent = describeThemeStatus(theme);
  }
  syncThemeControls();
}

function resolveThemeMode(now) {
  const hour = now.getHours();
  return hour >= 7 && hour < 19 ? "day" : "night";
}

function describeThemeStatus(theme) {
  const themeLabel = theme === "night" ? "黑夜模式" : "白天模式";
  return state.themePreference === "auto" ? `跟随时间 · ${themeLabel}` : `手动切换 · ${themeLabel}`;
}

function setThemePreference(mode) {
  const normalized = ["auto", "day", "night"].includes(mode) ? mode : "auto";
  state.themePreference = normalized;
  saveThemePreference(normalized);
  applyTheme(new Date());
}

function syncThemeControls() {
  refs.themeToggles.forEach((button) => {
    const active = (button.dataset.themeMode || "auto") === state.themePreference;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function loadThemePreference() {
  try {
    const saved = window.localStorage.getItem("theme-preference");
    return ["auto", "day", "night"].includes(saved) ? saved : "auto";
  } catch (error) {
    return "auto";
  }
}

function saveThemePreference(mode) {
  try {
    if (mode === "auto") {
      window.localStorage.removeItem("theme-preference");
      return;
    }
    window.localStorage.setItem("theme-preference", mode);
  } catch (error) {
    // Ignore storage failures; auto mode remains the fallback.
  }
}

function formatClock(now) {
  return `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
}

async function loadDataset() {
  try {
    const response = await fetch("./data/schedule-data.json", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    state.dataset = await response.json();
    const today = getBrowserToday();
    state.lastKnownToday = toIsoDate(today);
    state.week = clampWeek(dateToWeek(today), 1);
    state.weekday = today.getDay() === 0 ? 7 : today.getDay();
    state.scope = "all_day";
    state.date = toIsoDate(today);
    state.selectedPeople = allPeopleNames();
    renderWeekdayPills();
    renderPeopleFilters();
    syncQueryInputs();
    render();
  } catch (error) {
    refs.heroSummary.textContent = "静态数据加载失败，请确认 docs/data/schedule-data.json 已导出。";
    refs.datasetMeta.textContent = String(error);
    refs.summaryStrip.innerHTML = `<div class="empty-state">无法读取课表数据。</div>`;
  }
}

function render() {
  if (!state.dataset) return;
  const selectedPeople = selectedPeopleNames();
  const snapshot = buildSnapshot(state.scope, state.week, state.weekday, selectedPeople);
  const collaboration = buildCollaboration(selectedPeople, state.week);
  refs.heroSummary.textContent = `${state.date} · 第${state.week}周 · ${WEEKDAY_LABELS[state.weekday]} · ${SCOPE_LABELS[state.scope]} · 当前筛选 ${selectedPeople.length}/${allPeopleNames().length} 人`;
  refs.datasetMeta.textContent = `数据生成于 ${state.dataset.generated_at}，当前生效版本 #${state.dataset.current_import.id}`;
  refs.matrixMeta.textContent = `${state.date} / 第${state.week}周 / ${WEEKDAY_LABELS[state.weekday]} / ${SCOPE_LABELS[state.scope]}`;
  refs.peopleCount.textContent = `${selectedPeople.length}/${allPeopleNames().length} 人`;
  renderSummary(snapshot.summary);
  renderHeatmap(collaboration);
  renderRankings(collaboration);
  renderMatrix(snapshot);
}

function renderWeekdayPills() {
  refs.weekdayPills.innerHTML = "";
  for (let weekday = 1; weekday <= 7; weekday += 1) {
    const label = document.createElement("label");
    label.className = "weekday-pill";
    label.innerHTML = `
      <input type="radio" name="weekday" value="${weekday}" ${weekday === state.weekday ? "checked" : ""}>
      <span>${WEEKDAY_SHORT[weekday]}</span>
    `;
    const input = label.querySelector("input");
    input.addEventListener("change", () => {
      state.weekday = Number(input.value);
      state.date = toIsoDate(deriveDate(state.week, state.weekday));
      syncQueryInputs();
      render();
    });
    refs.weekdayPills.appendChild(label);
  }
}

function renderPeopleFilters() {
  refs.peopleFilters.innerHTML = "";
  for (const person of state.dataset.people) {
    const label = document.createElement("label");
    label.className = "person-chip";
    label.innerHTML = `
      <input type="checkbox" value="${escapeHtml(person.person_name)}" checked>
      <span>${escapeHtml(person.person_name)}</span>
    `;
    const input = label.querySelector("input");
    input.addEventListener("change", () => {
      state.selectedPeople = selectedPeopleNamesFromDom();
      render();
    });
    refs.peopleFilters.appendChild(label);
  }
}

function renderSummary(summary) {
  refs.summaryStrip.innerHTML = `
    <span class="summary-pill"><strong>当前 ${summary.counts.selected} 人</strong></span>
    <span class="summary-pill">全天空闲 ${summary.counts.all_free}</span>
    <span class="summary-pill">全天忙碌 ${summary.counts.all_busy}</span>
    <span class="summary-pill">有空闲时间 ${summary.counts.partial}</span>
    <span class="summary-pill">${escapeHtml(summary.groups.all_free.join("、") || "当前没有整段空闲的人")}</span>
  `;
}

function renderHeatmap(collaboration) {
  if (collaboration.totalPeople === 0) {
    refs.heatmapRoot.innerHTML = `<div class="empty-state">至少选择 1 个人后，才会生成热力图。</div>`;
    return;
  }
  const head = `<thead><tr><th>时段</th>${collaboration.rows.map((row) => `<th>${row.weekday_label}</th>`).join("")}</tr></thead>`;
  const body = ["morning", "afternoon", "evening"].map((scope) => {
    const cells = collaboration.rows.map((row) => {
      const item = row.items.find((entry) => entry.scope === scope);
      const heat = item.total_count === 0 ? 0 : item.free_count / item.total_count;
      return `
        <td>
          <a href="#" class="heat-cell" data-week="${item.week}" data-weekday="${item.weekday}" data-scope="${item.scope}" style="--heat:${heat.toFixed(3)}">
            <strong>${item.free_count}/${item.total_count}</strong>
            <span>完全空</span>
            <small>另 ${item.partial_count} 人有空档</small>
          </a>
        </td>
      `;
    }).join("");
    return `<tr><th>${SCOPE_LABELS[scope]}</th>${cells}</tr>`;
  }).join("");
  refs.heatmapRoot.innerHTML = `<table class="heatmap-table">${head}<tbody>${body}</tbody></table>`;
  refs.heatmapRoot.querySelectorAll(".heat-cell").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      state.week = Number(link.dataset.week);
      state.weekday = Number(link.dataset.weekday);
      state.scope = link.dataset.scope;
      state.date = toIsoDate(deriveDate(state.week, state.weekday));
      syncQueryInputs();
      render();
    });
  });
}

function renderRankings(collaboration) {
  if (collaboration.totalPeople === 0) {
    refs.rankingsRoot.innerHTML = `<div class="empty-state">至少选择 1 个人后，才会生成排行。</div>`;
    return;
  }
  refs.rankingsRoot.scrollTop = 0;
  refs.rankingsRoot.innerHTML = collaboration.rankings.map((item, index) => `
    <a href="#" class="ranking-card" data-week="${item.week}" data-weekday="${item.weekday}" data-scope="${item.scope}">
      <span class="ranking-index">${index + 1}</span>
      <div>
        <strong>${item.weekday_label} · ${item.scope_label}</strong>
        <p>${item.free_count}/${item.total_count} 人完全空，另 ${item.partial_count} 人有空档</p>
        <span class="muted">${escapeHtml(item.free_people.join("、") || "当前没有整段空闲的人")}</span>
      </div>
    </a>
  `).join("");
  refs.rankingsRoot.querySelectorAll(".ranking-card").forEach((card) => {
    card.addEventListener("click", (event) => {
      event.preventDefault();
      state.week = Number(card.dataset.week);
      state.weekday = Number(card.dataset.weekday);
      state.scope = card.dataset.scope;
      state.date = toIsoDate(deriveDate(state.week, state.weekday));
      syncQueryInputs();
      render();
    });
  });
}

function renderMatrix(snapshot) {
  refs.matrixHead.innerHTML = `
    <tr>
      <th class="matrix-period">节次</th>
      ${snapshot.people.map((person) => `<th>${escapeHtml(person.person_name)}<br><span class="muted">${escapeHtml(person.student_id)}</span></th>`).join("")}
    </tr>
  `;
  if (snapshot.people.length === 0) {
    refs.matrixBody.innerHTML = `<tr><td colspan="99"><div class="empty-state">当前没有选中任何人员。</div></td></tr>`;
    return;
  }
  refs.matrixBody.innerHTML = snapshot.periods.map((period) => {
    const periodDetail = state.dataset.period_details.find((item) => item.period === period);
    const cells = snapshot.people.map((person) => {
      const slot = person.slots.find((item) => item.period === period);
      if (!slot || slot.status === "free") {
        return `<td class="slot-free"><div class="slot-card"><strong>空闲</strong><small>${escapeHtml(slot ? slot.period_time : periodDetail.time)}</small></div></td>`;
      }
      return `
        <td class="slot-busy">
          <button type="button" class="course-btn" data-person="${escapeHtml(person.person_name)}" data-period="${period}">
            <strong>${escapeHtml(slot.course_name)}</strong>
            <span>${escapeHtml(slot.location || "地点待补充")}</span>
            <small>${escapeHtml(slot.course_time_text)}</small>
          </button>
        </td>
      `;
    }).join("");
    return `
      <tr>
        <th class="matrix-period">
          <div class="period-meta">
            <strong>${periodDetail.label}</strong>
            <small>${periodDetail.time}</small>
          </div>
        </th>
        ${cells}
      </tr>
    `;
  }).join("");
  refs.matrixBody.querySelectorAll(".course-btn").forEach((button) => {
    button.addEventListener("click", () => {
      const person = snapshot.people.find((item) => item.person_name === button.dataset.person);
      if (!person) return;
      const slot = person.slots.find((item) => String(item.period) === button.dataset.period);
      if (slot) {
        openDetail(slot);
      }
    });
  });
}

function renderNaturalResult(question) {
  const selectedPeople = selectedPeopleNames();
  const snapshot = buildSnapshot(state.scope, state.week, state.weekday, selectedPeople);
  refs.nlResult.innerHTML = `
    <div class="summary-pill"><strong>${escapeHtml(question)}</strong></div>
    <div class="status-grid">
      ${renderStatusCard("全天空闲", snapshot.summary.groups.all_free, "free")}
      ${renderStatusCard("全天忙碌", snapshot.summary.groups.all_busy, "busy")}
      ${renderStatusCard("有空闲时间", snapshot.summary.groups.partial, "")}
    </div>
  `;
}

function renderStatusCard(title, people, className) {
  return `
    <article class="status-card ${className}">
      <h3>${title}</h3>
      <div class="chip-grid">${people.length > 0 ? people.map((name) => `<span class="tag">${escapeHtml(name)}</span>`).join("") : '<span class="tag">暂无</span>'}</div>
    </article>
  `;
}

function buildSnapshot(scope, week, weekday, selectedPeople) {
  const selectedSet = new Set(selectedPeople);
  const periods = SCOPE_PERIODS[scope];
  const people = state.dataset.people
    .filter((person) => selectedSet.has(person.person_name))
    .map((person) => buildPersonSnapshot(person, week, weekday, periods));
  people.sort((left, right) => {
    if (left.busy_count !== right.busy_count) return left.busy_count - right.busy_count;
    return left.person_name.localeCompare(right.person_name, "zh-CN");
  });
  const groups = {
    all_free: people.filter((item) => item.availability_status === "all_free").map((item) => item.person_name),
    all_busy: people.filter((item) => item.availability_status === "all_busy").map((item) => item.person_name),
    partial: people.filter((item) => item.availability_status === "partial").map((item) => item.person_name),
  };
  return {
    people,
    periods,
    summary: {
      groups,
      counts: {
        selected: people.length,
        all_free: groups.all_free.length,
        all_busy: groups.all_busy.length,
        partial: groups.partial.length,
      },
    },
  };
}

function buildPersonSnapshot(person, week, weekday, periods) {
  const slotMap = new Map();
  for (const period of SCOPE_PERIODS.all_day) {
    slotMap.set(period, null);
  }
  const meetings = state.dataset.meetings.filter((meeting) => meeting.person_name === person.person_name && meeting.weekday === weekday && meeting.weeks.includes(week));
  for (const meeting of meetings) {
    for (let period = meeting.period_start; period <= meeting.period_end; period += 1) {
      slotMap.set(period, meeting);
    }
  }
  const slots = periods.map((period) => {
    const meeting = slotMap.get(period);
    return meeting
      ? {
          period,
          status: "busy",
          period_time: periodTime(period),
          course_name: meeting.course_name,
          location: meeting.location,
          teacher: meeting.teacher,
          course_code: meeting.course_code,
          weeks_text: meeting.weeks_text,
          course_time_text: meeting.course_time_text,
          period_start: meeting.period_start,
          period_end: meeting.period_end,
        }
      : {
          period,
          status: "free",
          period_time: periodTime(period),
          course_name: "",
          location: "",
          teacher: "",
          course_code: "",
          weeks_text: "",
          course_time_text: periodTime(period),
          period_start: period,
          period_end: period,
        };
  });
  const freeCount = slots.filter((slot) => slot.status === "free").length;
  let availabilityStatus = "partial";
  if (freeCount === slots.length) {
    availabilityStatus = "all_free";
  } else if (freeCount === 0) {
    availabilityStatus = "all_busy";
  }
  return {
    person_name: person.person_name,
    student_id: person.student_id,
    availability_status: availabilityStatus,
    free_count: freeCount,
    busy_count: slots.length - freeCount,
    slots,
  };
}

function buildCollaboration(selectedPeople, week) {
  const rows = [];
  const rankings = [];
  for (let weekday = 1; weekday <= 5; weekday += 1) {
    const items = ["morning", "afternoon", "evening"].map((scope) => {
      const snapshot = buildSnapshot(scope, week, weekday, selectedPeople);
      const groups = snapshot.summary.groups;
      const item = {
        week,
        weekday,
        weekday_label: WEEKDAY_LABELS[weekday],
        scope,
        scope_label: SCOPE_LABELS[scope],
        date: toIsoDate(deriveDate(week, weekday)),
        total_count: snapshot.people.length,
        free_count: groups.all_free.length,
        busy_count: groups.all_busy.length,
        partial_count: groups.partial.length,
        free_people: groups.all_free.slice(),
      };
      rankings.push(item);
      return item;
    });
    rows.push({ weekday, weekday_label: WEEKDAY_LABELS[weekday], items });
  }
  rankings.sort((left, right) => {
    if (right.free_count !== left.free_count) return right.free_count - left.free_count;
    if (right.partial_count !== left.partial_count) return right.partial_count - left.partial_count;
    if (left.weekday !== right.weekday) return left.weekday - right.weekday;
    return ["morning", "afternoon", "evening"].indexOf(left.scope) - ["morning", "afternoon", "evening"].indexOf(right.scope);
  });
  return {
    totalPeople: selectedPeople.length,
    rows,
    rankings: rankings.slice(0, 10),
  };
}

function openDetail(slot) {
  refs.detailTitle.textContent = slot.course_name;
  refs.detailContent.innerHTML = [
    detailBlock("课程号", slot.course_code || "未解析"),
    detailBlock("开课周次", slot.weeks_text || "未解析"),
    detailBlock("授课老师", slot.teacher || "未提供"),
    detailBlock("上课场地", slot.location || "未提供"),
    detailBlock("节次", slot.period_start === slot.period_end ? `第${slot.period_start}节` : `第${slot.period_start}-${slot.period_end}节`),
    detailBlock("上课时间", slot.course_time_text || "未配置"),
  ].join("");
  refs.detailModal.classList.add("open");
  refs.detailModal.setAttribute("aria-hidden", "false");
}

function closeDetail() {
  refs.detailModal.classList.remove("open");
  refs.detailModal.setAttribute("aria-hidden", "true");
}

function detailBlock(label, value) {
  return `<div class="detail-block"><strong>${escapeHtml(label)}</strong><div>${escapeHtml(value)}</div></div>`;
}

function syncQueryInputs() {
  refs.queryDate.value = state.date;
  refs.queryWeek.value = state.week;
  refs.queryScope.value = state.scope;
  refs.weekdayPills.querySelectorAll("input").forEach((input) => {
    input.checked = Number(input.value) === state.weekday;
  });
}

function syncPeopleChecks() {
  const selectedSet = new Set(state.selectedPeople);
  refs.peopleFilters.querySelectorAll("input").forEach((input) => {
    input.checked = selectedSet.has(input.value);
  });
}

function selectedPeopleNames() {
  const names = selectedPeopleNamesFromDom();
  state.selectedPeople = names;
  return names;
}

function selectedPeopleNamesFromDom() {
  return Array.from(refs.peopleFilters.querySelectorAll("input"))
    .filter((input) => input.checked)
    .map((input) => input.value);
}

function allPeopleNames() {
  return state.dataset ? state.dataset.people.map((person) => person.person_name) : [];
}

function jumpRelative(offsetDays) {
  if (!state.dataset) return;
  const base = getBrowserToday();
  base.setDate(base.getDate() + offsetDays);
  state.date = toIsoDate(base);
  state.week = clampWeek(dateToWeek(base), 1);
  state.weekday = base.getDay() === 0 ? 7 : base.getDay();
  syncQueryInputs();
  render();
}

function parseNaturalQuestion(question) {
  const text = question.trim();
  const parsed = {
    scope: "all_day",
    people_names: allPeopleNames().filter((name) => text.includes(name)),
  };
  if (text.includes("上午")) {
    parsed.scope = "morning";
  } else if (text.includes("下午")) {
    parsed.scope = "afternoon";
  } else if (text.includes("晚上")) {
    parsed.scope = "evening";
  }
  const explicit = parseExplicitDate(text);
  if (explicit) {
    parsed.date = toIsoDate(explicit);
    parsed.week = dateToWeek(explicit);
    parsed.weekday = explicit.getDay() === 0 ? 7 : explicit.getDay();
    return parsed;
  }
  if (text.includes("今天")) {
    const today = getBrowserToday();
    parsed.date = toIsoDate(today);
    parsed.week = dateToWeek(today);
    parsed.weekday = today.getDay() === 0 ? 7 : today.getDay();
    return parsed;
  }
  if (text.includes("明天")) {
    const tomorrow = getBrowserToday();
    tomorrow.setDate(tomorrow.getDate() + 1);
    parsed.date = toIsoDate(tomorrow);
    parsed.week = dateToWeek(tomorrow);
    parsed.weekday = tomorrow.getDay() === 0 ? 7 : tomorrow.getDay();
    return parsed;
  }
  const weekMatch = text.match(/第?\s*(\d+)\s*周/);
  if (weekMatch) {
    parsed.week = clampWeek(Number(weekMatch[1]) || 1, 1);
  } else if (text.includes("本周") || text.includes("这周")) {
    parsed.week = clampWeek(dateToWeek(getBrowserToday()), 1);
  } else if (text.includes("下周")) {
    parsed.week = clampWeek(dateToWeek(getBrowserToday()) + 1, 1);
  }
  const weekdayMatch = text.match(/(?:周|星期)([一二三四五六日天])/);
  if (weekdayMatch) {
    parsed.weekday = WEEKDAY_ALIAS[weekdayMatch[1]];
  }
  if (parsed.week && parsed.weekday) {
    parsed.date = toIsoDate(deriveDate(parsed.week, parsed.weekday));
  }
  return parsed;
}

function parseExplicitDate(text) {
  const isoMatch = text.match(/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})/);
  if (isoMatch) {
    return new Date(Number(isoMatch[1]), Number(isoMatch[2]) - 1, Number(isoMatch[3]));
  }
  const monthDay = text.match(/(?:(20\d{2})年)?\s*(\d{1,2})月(\d{1,2})[日号]?/);
  if (monthDay) {
    const year = Number(monthDay[1] || getBrowserToday().getFullYear());
    return new Date(year, Number(monthDay[2]) - 1, Number(monthDay[3]));
  }
  return null;
}

function deriveDate(week, weekday) {
  const semesterStart = fromIsoDate(state.dataset.semester_start_date);
  const next = new Date(semesterStart);
  next.setDate(semesterStart.getDate() + ((week - 1) * 7) + weekday - 1);
  return next;
}

function dateToWeek(dateValue) {
  const semesterStart = fromIsoDate(state.dataset.semester_start_date);
  const diffDays = Math.floor((stripTime(dateValue) - stripTime(semesterStart)) / 86400000);
  if (diffDays < 0) return 0;
  return Math.floor(diffDays / 7) + 1;
}

function clampWeek(week, minimum) {
  const maxWeek = state.dataset ? Math.max(state.dataset.max_week || week, minimum) : week;
  return Math.min(Math.max(week, minimum), maxWeek);
}

function fromIsoDate(value) {
  if (!value) return null;
  const parts = String(value).split("-");
  if (parts.length !== 3) return null;
  return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
}

function getBrowserToday() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function toIsoDate(dateValue) {
  return `${dateValue.getFullYear()}-${String(dateValue.getMonth() + 1).padStart(2, "0")}-${String(dateValue.getDate()).padStart(2, "0")}`;
}

function stripTime(dateValue) {
  return new Date(dateValue.getFullYear(), dateValue.getMonth(), dateValue.getDate()).getTime();
}

function periodTime(period) {
  const detail = state.dataset.period_details.find((item) => item.period === period);
  return detail ? detail.time : "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;")
    .replaceAll("'", "&#39;");
}
