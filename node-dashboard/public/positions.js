const byId = (id) => document.getElementById(id);
const money = (value) =>
  value == null || Number.isNaN(Number(value))
    ? '--'
    : `${Number(value) < 0 ? '-' : ''}₹${Math.abs(Number(value)).toLocaleString('en-IN', {
        maximumFractionDigits: 2,
      })}`;
const num = (value) =>
  value == null || Number.isNaN(Number(value))
    ? '--'
    : Number(value).toLocaleString('en-IN', { maximumFractionDigits: 2 });

const sign = (value) => (Number(value) > 0 ? 'good' : Number(value) < 0 ? 'danger' : '');

const contractText = (position) =>
  (position.legs || [])
    .map((leg) => leg.symbol || `${position.instrument} ${num(leg.strike)} ${leg.optionType}`)
    .join('<br>');

const legText = (position) =>
  (position.legs || [])
    .map((leg) => `${leg.quantity || 1}x ${num(leg.strike)} ${leg.optionType} @ ${num(leg.entry)}`)
    .join('<br>');

const targetText = (position) =>
  (position.targets || [])
    .map(
      (target) =>
        `<span class="${target.hit ? 'good' : 'muted'}">${target.label} ${num(target.premium)}${
          target.hit ? ' ✓' : ''
        }</span>`,
    )
    .join('<br>');

function breakdown(rows, elementId) {
  const target = byId(elementId);
  if (!target) return;
  target.innerHTML = rows?.length
    ? rows
        .map(
          (row) => `<tr><td>${row.name}</td><td>${row.trades}</td><td>${row.wins}</td><td>${row.losses}</td>
            <td>${num(row.hitRate)}%</td><td class="${sign(row.pnl)}"><b>${money(row.pnl)}</b></td></tr>`,
        )
        .join('')
    : '<tr><td colspan="6" class="muted">No closed trades yet</td></tr>';
}

function render(data) {
  const summary = data.summary || {};
  byId('openPnl').textContent = money(summary.openPnl);
  byId('openPnl').className = sign(summary.openPnl);
  byId('bookedPnl').textContent = money(summary.bookedPnl);
  byId('bookedPnl').className = sign(summary.bookedPnl);
  byId('totalPnl').textContent = money(summary.totalPnl);
  byId('totalPnl').className = sign(summary.totalPnl);
  byId('counts').textContent = `${summary.openCount ?? 0} / ${summary.closedCount ?? 0}`;
  const hit = byId('hitRate');
  if (hit) hit.textContent = summary.hitRate == null ? '--' : `${num(summary.hitRate)}%`;
  byId('statusText').textContent = 'live';
  byId('statusDot').className = 'live';
  breakdown(data.byStrategy, 'strategyRows');
  breakdown(data.byGrade, 'gradeRows');
  breakdown(data.byExpiry, 'expiryRows');

  const open = data.open || [];
  byId('openRows').innerHTML = open.length
    ? open
        .map(
          (position) => `<tr>
            <td><b>${contractText(position)}</b><br><span class="muted">${position.instrument} &middot; ${
              position.expiry || ''
            }</span></td>
            <td>${position.signal || position.strategy || ''}${
              position.grade ? `<br><span class="muted">grade ${position.grade}</span>` : ''
            }</td>
            <td>${legText(position)}</td>
            <td>${position.lots} lot<br><span class="muted">${position.lotSize} qty</span></td>
            <td>${num(position.entryPremium)}<br><span class="muted">sugg ${num(position.suggestedEntry)}</span></td>
            <td>${num(position.lastPremium)}</td>
            <td class="danger">${num(position.activeStop ?? position.stopPremium)}${
              position.activeStop > (position.stopPremium ?? 0) ? '<br><span class="good">trailed</span>' : ''
            }</td>
            <td>${targetText(position)}</td>
            <td class="${sign(position.pnl)}"><b>${money(position.pnl)}</b><br><span class="muted">${num(
              position.pnlPercent,
            )}%</span></td>
            <td><button class="book-btn" data-id="${position.id}">Book P&amp;L</button></td>
          </tr>`,
        )
        .join('')
    : '<tr><td colspan="10" class="muted">No open paper positions</td></tr>';

  const closed = data.closed || [];
  byId('closedRows').innerHTML = closed.length
    ? closed
        .map((position) => {
          const pnl = (position.closePremium - position.entryPremium) * position.lotSize * position.lots;
          return `<tr>
            <td><b>${contractText(position)}</b><br><span class="muted">${position.instrument}</span></td>
            <td>${position.signal || position.strategy || ''}${
              position.grade ? `<br><span class="muted">grade ${position.grade}</span>` : ''
            }</td>
            <td>${num(position.entryPremium)}</td>
            <td>${num(position.closePremium)}</td>
            <td>${position.exitReason || ''}</td>
            <td class="${sign(pnl)}"><b>${money(pnl)}</b></td>
          </tr>`;
        })
        .join('')
    : '<tr><td colspan="6" class="muted">Nothing closed yet</td></tr>';
}

async function poll() {
  try {
    const response = await fetch('/api/positions');
    render(await response.json());
  } catch {
    byId('statusText').textContent = 'disconnected';
    byId('statusDot').className = '';
  }
}

poll();
setInterval(poll, 2000);

document.addEventListener('click', async (event) => {
  const button = event.target.closest('.book-btn');
  if (!button) return;
  button.disabled = true;
  button.textContent = 'Booking...';
  try {
    const response = await fetch(`/api/positions/${button.dataset.id}/close`, { method: 'POST' });
    const result = await response.json();
    if (!result.closed) {
      button.disabled = false;
      button.textContent = 'Book P&L';
      return;
    }
  } catch {
    button.disabled = false;
    button.textContent = 'Book P&L';
    return;
  }
  poll();
});
