function candleChart(candles, levels = [], emptyText = 'Waiting for candles', label = 'Candlestick chart') {
  if (!candles?.length) return `<p class="muted chart-empty">${emptyText}</p>`;

  const width = 760;
  const height = 300;
  const padLeft = 8;
  const padRight = 92;
  const padY = 16;

  const usable = levels.filter((level) => typeof level.value === 'number' && !Number.isNaN(level.value));
  const highs = candles.map((c) => Number(c.high));
  const lows = candles.map((c) => Number(c.low));
  const values = [...highs, ...lows, ...usable.map((l) => l.value)];
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min || 1;
  const pad = span * 0.06;
  const top = max + pad;
  const bottom = min - pad;

  const plotWidth = width - padLeft - padRight;
  const y = (value) => padY + ((top - value) / (top - bottom)) * (height - 2 * padY);
  const slot = plotWidth / candles.length;
  const bodyWidth = Math.max(2, Math.min(11, slot * 0.62));

  const bars = candles
    .map((candle, index) => {
      const cx = padLeft + slot * (index + 0.5);
      const open = Number(candle.open);
      const close = Number(candle.close);
      const rising = close >= open;
      const color = rising ? '#147d77' : '#df5d3f';
      const bodyTop = y(Math.max(open, close));
      const bodyHeight = Math.max(1, Math.abs(y(open) - y(close)));
      return `<line x1="${cx}" y1="${y(Number(candle.high))}" x2="${cx}" y2="${y(Number(candle.low))}" stroke="${color}" stroke-width="1"/>
        <rect x="${cx - bodyWidth / 2}" y="${bodyTop}" width="${bodyWidth}" height="${bodyHeight}" fill="${
          rising ? color : '#fff'
        }" stroke="${color}" stroke-width="1"/>`;
    })
    .join('');

  const lines = usable
    .map((level) => {
      const ly = y(level.value);
      const dash = level.label === 'Entry' ? '' : 'stroke-dasharray="4 3"';
      return `<line x1="${padLeft}" y1="${ly}" x2="${padLeft + plotWidth}" y2="${ly}" stroke="${level.color}" stroke-width="1.2" ${dash}/>
        <text x="${padLeft + plotWidth + 6}" y="${ly + 3.5}" fill="${level.color}" font-size="10" font-family="'Trebuchet MS', sans-serif">${
          level.label
        } ${level.value.toLocaleString('en-IN')}</text>`;
    })
    .join('');

  return `<svg viewBox="0 0 ${width} ${height}" class="chart" preserveAspectRatio="xMidYMid meet" role="img"
      aria-label="${label}">${bars}${lines}</svg>`;
}

function premiumLevels(setup) {
  if (setup.state !== 'setup') return [];
  const levels = [
    { label: 'Entry', value: setup.netPremium, color: '#182126' },
    { label: 'Stop', value: setup.stopLoss?.netPremium, color: '#df5d3f' },
  ];
  for (const target of setup.targets || []) {
    levels.push({ label: target.label, value: target.netPremium, color: '#147d77' });
  }
  return levels;
}

const byId = (id) => document.getElementById(id);
const num = (value, digits = 2) =>
  value == null || Number.isNaN(Number(value))
    ? '--'
    : Number(value).toLocaleString('en-IN', { maximumFractionDigits: digits });

const spotText = (value) =>
  value == null ? '--' : typeof value === 'object' ? `${num(value.lower)} / ${num(value.upper)}` : num(value);

function contextRow(setup) {
  const c = setup.context || {};
  const cells = [
    ['Spot', num(c.spot)],
    ['EMA 9', num(c.ema9)],
    ['EMA 15', num(c.ema15)],
    ['VWAP', num(c.vwap)],
    ['Supertrend', c.supertrend == null ? '--' : `${num(c.supertrend)} (${c.supertrendDirection || '--'})`],
    ['ORB high', num(c.orbHigh)],
    ['ORB low', num(c.orbLow)],
    ['ATR', num(c.atr)],
    ['Days to expiry', num(c.daysToExpiry, 1)],
    [
      c.volatility?.source || 'India VIX',
      c.volatility?.value == null ? '--' : `${num(c.volatility.value)} (${c.vixRegime || '--'})`,
    ],
    ['PCR', num(c.pcr)],
    ['Call wall', num(c.callWall)],
    ['Put wall', num(c.putWall)],
  ];
  return `<div class="context">${cells
    .map(([label, value]) => `<div><span>${label}</span><b>${value}</b></div>`)
    .join('')}</div>`;
}

function legsTable(setup) {
  if (!setup.legs?.length) return '';
  return `<table class="legs"><thead><tr><th>Action</th><th>Qty</th><th>Strike</th><th>Type</th><th>LTP</th><th>Entry range</th><th>IV %</th><th>Delta</th><th>Theta/day</th></tr></thead><tbody>
    ${setup.legs
      .map(
        (leg) => `<tr class="${leg.action === 'BUY' ? 'buy' : 'sell'}">
          <td><span class="tag">${leg.action}</span></td><td>${leg.quantity || 1}x</td><td>${num(leg.strike)}</td><td>${leg.optionType}</td>
          <td>${num(leg.ltp)}</td><td>${num(leg.entryRange?.[0])} - ${num(leg.entryRange?.[1])}</td>
          <td>${num(leg.iv)}</td><td>${num(leg.delta, 3)}</td><td>${num(leg.thetaPerDay)}</td></tr>`,
      )
      .join('')}
  </tbody></table>`;
}

const CANDIDATE_LABELS = {
  selected: ['selected', 'good'],
  qualified: ['qualified', ''],
  rejected: ['rejected', 'danger'],
  blocked: ['blocked', 'danger'],
  'not-triggered': ['not triggered', 'muted'],
};

function strategyTable(setup) {
  const candidates = setup.candidates || [];
  if (!candidates.length) return '';
  const order = { selected: 0, qualified: 1, rejected: 2, blocked: 3, 'not-triggered': 4 };
  const rows = [...candidates].sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9));
  return `<table class="legs strategies"><thead><tr><th>Strategy</th><th>Side</th><th>Grade</th><th>R:R</th><th>Status</th></tr></thead><tbody>
    ${rows
      .map((candidate) => {
        const [label, tone] = CANDIDATE_LABELS[candidate.state] || [candidate.state, ''];
        return `<tr><td>${candidate.name}</td><td>${candidate.direction || '--'}</td><td>${
          candidate.grade || '--'
        }</td><td>${num(candidate.riskReward)}</td><td class="${tone}">${label}${
          candidate.reason ? ` &middot; ${candidate.reason}` : ''
        }</td></tr>`;
      })
      .join('')}
  </tbody></table>`;
}

function setupCard(setup) {
  const view = (setup.view || 'Neutral').toLowerCase();
  if (setup.state !== 'setup') {
    return `<article class="panel setup blocked">
      <div class="panel-heading"><span class="eyebrow">${setup.instrument || '--'}</span><span class="state flat">NO TRADE</span></div>
      <h2>No qualifying setup</h2>
      <ul class="reasons">${(setup.reasons || []).map((reason) => `<li>${reason}</li>`).join('')}</ul>
      <h3>Strategy scan</h3>
      ${strategyTable(setup)}
      ${setup.context ? contextRow(setup) : ''}
    </article>`;
  }

  const greeks = setup.positionGreeks || {};
  return `<article class="panel setup">
    <div class="panel-heading">
      <span class="eyebrow">${setup.instrument} &middot; ${setup.expiry || '--'}</span>
      <span class="state ${view}">${(setup.view || '').toUpperCase()}</span>
    </div>
    <h2>${setup.strategy}</h2>
    <p class="muted structure">${setup.structure} &middot; lot size ${num(setup.lotSize, 0)} &middot; net ${setup.premiumType} ${num(setup.netPremium)}</p>
    ${
      setup.grade
        ? `<p class="grade">Grade <b>${setup.grade}</b>${
            setup.confluence?.length > 1 ? ` &middot; ${setup.confluence.length} strategies agree` : ''
          }</p><ul class="reasons">${(setup.gradeFactors || [])
            .map((factor) => `<li>${factor}</li>`)
            .join('')}</ul>`
        : ''
    }

    <h3>Technical &amp; OI trigger</h3>
    <ul class="reasons">${(setup.trigger || []).map((item) => `<li>${item}</li>`).join('')}</ul>
    ${contextRow(setup)}

    <h3>Strategy scan</h3>
    ${strategyTable(setup)}

    <h3>Trade legs</h3>
    ${legsTable(setup)}

    <h3>${setup.chart?.symbol || 'Selected strike'} &middot; premium candles</h3>
    ${candleChart(
      setup.chart?.candles,
      premiumLevels(setup),
      'Waiting for candles on the selected strike',
      `${setup.chart?.symbol || 'Selected strike'} premium candles with entry, stop loss and target levels`,
    )}

    <div class="levels">
      <div class="stop"><span>Stop loss (spot)</span><b>${spotText(setup.stopLoss?.spot)}</b></div>
      <div class="stop"><span>Stop loss (premium)</span><b>${num(setup.stopLoss?.netPremium)}</b></div>
      ${(setup.targets || [])
        .map(
          (target) =>
            `<div class="target"><span>${target.label} &middot; spot ${spotText(target.spot)}</span><b>${num(
              target.netPremium,
            )}</b><i>+${num(target.profitPerLot)}/lot</i></div>`,
        )
        .join('')}
    </div>
    <p class="muted">${setup.stopLoss?.note || ''}</p>

    <div class="risk">
      <div><span>Risk to stop / lot</span><b class="danger">${num(setup.maxRiskPerLot)}</b></div>
      <div><span>Premium outlay / lot</span><b>${num(setup.premiumAtRiskPerLot)}</b></div>
      <div><span>Reward at T3 / lot</span><b class="good">${num(setup.maxRewardPerLot)}</b></div>
      <div><span>R:R at T2</span><b>${num(setup.riskReward)}</b></div>
      <div><span>Net delta</span><b>${num(greeks.netDelta, 3)}</b></div>
      <div><span>Net theta / day</span><b>${num(greeks.netThetaPerDay)}</b></div>
    </div>

    <h3>Adjustment &amp; exit plan</h3>
    <ol class="plan">${(setup.adjustmentPlan || []).map((step) => `<li>${step}</li>`).join('')}</ol>
  </article>`;
}

function render(payload) {
  const stale = payload.status === 'stale';
  byId('statusText').textContent = stale
    ? `stale feed (${num(payload.feedAgeSeconds, 0)}s)`
    : payload.status || 'offline';
  byId('statusDot').className =
    payload.status === 'live' ? 'live' : payload.status === 'demo' ? 'demo' : stale ? 'stale' : '';
  byId('vix').textContent = num(payload.vix);
  byId('tracked').textContent = num(payload.trackedInstruments, 0);
  const pnl = payload.paper?.totalPnl;
  const pnlValue = byId('pnlValue');
  if (pnlValue) {
    pnlValue.textContent =
      pnl == null ? '--' : `${pnl < 0 ? '-' : ''}₹${Math.abs(pnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
    pnlValue.className = pnl > 0 ? 'good' : pnl < 0 ? 'danger' : '';
  }
  byId('updated').textContent = payload.generatedAt
    ? new Date(payload.generatedAt).toLocaleTimeString('en-IN')
    : '--';

  const setups = payload.setups || [];
  byId('setups').innerHTML = setups.length
    ? setups.map((setup) => setupCard(setup)).join('')
    : `<article class="panel"><div class="panel-heading"><span class="eyebrow">NO INDEX DATA</span><span class="state flat">${(
        payload.status || 'offline'
      ).toUpperCase()}</span></div>
       <h2>Waiting for the first index ticks</h2>
       <ul class="reasons">
         <li>NSE cash and F&amp;O trade 09:15&ndash;15:30 IST; outside those hours the feed stays idle.</li>
         <li>Tracked instruments receiving data: ${num(payload.trackedInstruments, 0)}</li>
         ${payload.error ? `<li>${payload.error}</li>` : ''}
       </ul></article>`;

  const contexts = Object.fromEntries((payload.setups || []).map((setup) => [setup.instrument, setup.context || {}]));
  const charts = Object.entries(payload.underlyings || {})
    .map(([name, data]) => {
      const context = contexts[name] || {};
      const levels = [
        { label: 'EMA9', value: context.ema9, color: '#718087' },
        { label: 'EMA15', value: context.ema15, color: '#182126' },
        { label: 'VWAP', value: context.vwap, color: '#c9922f' },
        { label: 'ST', value: context.supertrend, color: '#147d77' },
        { label: 'ORB H', value: context.orbHigh, color: '#df5d3f' },
        { label: 'ORB L', value: context.orbLow, color: '#df5d3f' },
      ];
      return `<figure class="index-chart">
        <figcaption>${name} <b>${num(data.spot)}</b></figcaption>
        ${candleChart(data.candles, levels, 'Waiting for index candles', `${name} session candles with EMA and VWAP`)}
      </figure>`;
    })
    .join('');
  byId('indexCharts').innerHTML = charts || '<p class="muted">Waiting for index candles</p>';
}

const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
socket.onmessage = (event) => render(JSON.parse(event.data));
socket.onclose = () => {
  byId('statusText').textContent = 'Disconnected';
  byId('statusDot').className = '';
};
