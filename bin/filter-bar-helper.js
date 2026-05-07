// ═══════════════════════════════════════════════════════════════
// SHARED FILTER BAR HELPER
// ═══════════════════════════════════════════════════════════════
// Consolidates filter bar UI generation and event handling
// used across all dashboards. Replaces duplicate code in:
// - appServicePlan, containerApps, cosmos, devops, healthChecks,
//   reservedInstances, virtualMachines, eventhubs, azureCosts
//
// Expected globals (set by dashboard before calling these functions):
// - FILTER_DEFS: Array of {key, label, icon}
// - FILTER_OPTIONS: Object with date_range and filter option arrays
// - ACTIVE_FILTERS: Object initialized with Set values
//
// Key Functions:
// - buildFilterBar() - generates filter bar UI
// - onDateFilterChange(fromInput, toInput) - validates & updates date filter
// - onFilterCheck(filterKey) - updates checkbox filter state

function buildFilterBar() {
  const bar = document.getElementById('filterBar');
  if (!bar) return; // No filter bar in this dashboard
  
  bar.innerHTML = '<span class="filter-label">Filters</span>';

  // Build date range picker if date_range is available
  const dateRange = FILTER_OPTIONS.date_range;
  if (dateRange && dateRange.min && dateRange.max) {
    const dateGroup = document.createElement('div');
    dateGroup.className = 'filter-group filter-date-group';
    dateGroup.dataset.key = 'date';
    dateGroup.innerHTML = `
      <div class="filter-btn filter-btn-static">
        📅 Date
        <span class="date-range-inline">
          <input type="date" class="date-input" data-role="from" min="${dateRange.min}" max="${dateRange.max}" value="${ACTIVE_FILTERS.date_from || dateRange.min}">
          <span class="date-sep">to</span>
          <input type="date" class="date-input" data-role="to" min="${dateRange.min}" max="${dateRange.max}" value="${ACTIVE_FILTERS.date_to || dateRange.max}">
        </span>
      </div>`;

    const fromInput = dateGroup.querySelector('input[data-role="from"]');
    const toInput = dateGroup.querySelector('input[data-role="to"]');
    fromInput.addEventListener('change', () => onDateFilterChange(fromInput, toInput));
    toInput.addEventListener('change', () => onDateFilterChange(fromInput, toInput));
    bar.appendChild(dateGroup);
  }

  // Build filter groups from FILTER_DEFS
  FILTER_DEFS.forEach(def => {
    const options = FILTER_OPTIONS[def.key] || [];
    if (!options.length) return;
    
    const active = ACTIVE_FILTERS[def.key];
    const group = document.createElement('div');
    group.className = 'filter-group';
    group.dataset.key = def.key;

    const countLabel = active.size === 0 ? 'All' : active.size === options.length ? 'All' : `${active.size}/${options.length}`;

    group.innerHTML = `
      <button class="filter-btn">
        ${def.icon} ${def.label} <span class="count">${countLabel}</span> <span class="arrow">▼</span>
      </button>
      <div class="filter-dropdown">
        <div class="filter-actions">
          <button data-action="all">Select All</button>
          <button data-action="none">Select None</button>
          <button data-action="invert">Invert</button>
        </div>
        <div class="filter-search"><input type="text" placeholder="Search..."></div>
        <div class="filter-list"></div>
      </div>`;

    const list = group.querySelector('.filter-list');
    const isAll = active.size === 0;

    // Build filter items (checkboxes)
    options.forEach(opt => {
      const item = document.createElement('div');
      item.className = 'filter-item';
      item.innerHTML = `<input type="checkbox" value="${esc(opt)}" ${isAll || active.has(opt) ? 'checked' : ''}><label title="${esc(opt)}">${esc(opt)}</label>`;
      const cb = item.querySelector('input');
      cb.addEventListener('change', () => onFilterCheck(def.key));
      item.querySelector('label').addEventListener('click', (e) => {
        e.preventDefault();
        cb.checked = !cb.checked;
        onFilterCheck(def.key);
      });
      list.appendChild(item);
    });

    // Toggle dropdown
    group.querySelector('.filter-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = group.classList.contains('open');
      document.querySelectorAll('.filter-group').forEach(g => g.classList.remove('open'));
      if (!wasOpen) group.classList.add('open');
    });

    // Search within filter list
    group.querySelector('.filter-search input').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      list.querySelectorAll('.filter-item').forEach(item => {
        const v = item.querySelector('input').value.toLowerCase();
        item.style.display = v.includes(q) ? '' : 'none';
      });
    });

    // All/None/Invert buttons
    group.querySelectorAll('.filter-actions button').forEach(btn => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.action;
        const cbs = list.querySelectorAll('input[type=checkbox]');
        if (action === 'all') cbs.forEach(cb => { if (cb.closest('.filter-item').style.display !== 'none') cb.checked = true; });
        else if (action === 'none') cbs.forEach(cb => { if (cb.closest('.filter-item').style.display !== 'none') cb.checked = false; });
        else if (action === 'invert') cbs.forEach(cb => { if (cb.closest('.filter-item').style.display !== 'none') cb.checked = !cb.checked; });
        onFilterCheck(def.key);
      });
    });

    bar.appendChild(group);
  });

  // Reset all button
  const reset = document.createElement('button');
  reset.className = 'filter-reset-all';
  reset.textContent = 'Reset All';
  reset.addEventListener('click', () => {
    FILTER_DEFS.forEach(def => ACTIVE_FILTERS[def.key] = new Set());
    ACTIVE_FILTERS.date_from = '';
    ACTIVE_FILTERS.date_to = '';
    buildFilterBar();
    applyFilters();
  });
  bar.appendChild(reset);

  // Close dropdowns on background click
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.filter-group')) {
      document.querySelectorAll('.filter-group').forEach(g => g.classList.remove('open'));
    }
  });
}

/**
 * Handles date range input validation and update
 * @param {HTMLInputElement} fromInput - "from" date input
 * @param {HTMLInputElement} toInput - "to" date input
 */
function onDateFilterChange(fromInput, toInput) {
  let from = fromInput.value || '';
  let to = toInput.value || '';
  
  // Prevent from > to
  if (from && to && from > to) {
    if (document.activeElement === fromInput) {
      to = from;
      toInput.value = to;
    } else {
      from = to;
      fromInput.value = from;
    }
  }
  
  ACTIVE_FILTERS.date_from = from;
  ACTIVE_FILTERS.date_to = to;
  applyFilters();
}

/**
 * Handles filter checkbox changes
 * @param {string} filterKey - The filter key (from FILTER_DEFS)
 */
function onFilterCheck(filterKey) {
  const group = document.querySelector(`.filter-group[data-key="${filterKey}"]`);
  const options = FILTER_OPTIONS[filterKey] || [];
  const selected = new Set();
  
  group.querySelectorAll('.filter-list input[type=checkbox]').forEach(cb => {
    if (cb.checked) selected.add(cb.value);
  });

  // Empty set means "all" (no filtering)
  if (selected.size === 0 || selected.size === options.length) {
    ACTIVE_FILTERS[filterKey] = new Set();
  } else {
    ACTIVE_FILTERS[filterKey] = selected;
  }

  // Update count badge
  const active = ACTIVE_FILTERS[filterKey];
  group.querySelector('.count').textContent = active.size === 0 ? 'All' : `${active.size}/${options.length}`;
  
  applyFilters();
}

/**
 * Utility function to escape HTML special characters
 */
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
