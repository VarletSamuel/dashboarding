/**
 * table-sort.js — shared column sort utility
 * Adds click-to-sort on every <thead><th> across all dashboards.
 * Sorts the <tbody> rows; handles numeric, date (ISO), and string columns.
 * A data-v attribute on a <td> overrides textContent for comparison.
 */
(function () {
  document.addEventListener('click', function (e) {
    const th = e.target.closest('thead th');
    if (!th) return;
    _sortByTh(th);
  });

  function _sortByTh(th) {
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    if (!tbody) return;

    const ths = Array.from(th.closest('tr').querySelectorAll('th'));
    const col = ths.indexOf(th);
    const asc = th.getAttribute('data-sort') !== 'asc';

    // Clear all headers, set current
    ths.forEach(function (h) { h.removeAttribute('data-sort'); });
    th.setAttribute('data-sort', asc ? 'asc' : 'desc');

    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort(function (a, b) {
      const ta = _cellVal(a, col);
      const tb = _cellVal(b, col);
      const na = _toNum(ta);
      const nb = _toNum(tb);
      var cmp;
      if (!isNaN(na) && !isNaN(nb)) {
        cmp = na - nb;
      } else {
        cmp = ta.localeCompare(tb, undefined, { sensitivity: 'base', numeric: true });
      }
      return asc ? cmp : -cmp;
    });

    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  function _cellVal(row, idx) {
    var cell = row.cells[idx];
    if (!cell) return '';
    return (cell.getAttribute('data-v') || cell.textContent || '').trim();
  }

  function _toNum(s) {
    // Strip currency symbols, %, commas, spaces — keep digits, dot, minus
    return parseFloat(s.replace(/[^0-9.-]/g, ''));
  }
}());
