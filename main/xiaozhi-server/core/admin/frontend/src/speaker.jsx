import {useEffect, useLayoutEffect, useMemo, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Button} from '@astryxdesign/core/Button';
import {Heading} from '@astryxdesign/core/Heading';

const PLACEHOLDER_STATUS = new Set(['有可用实体', '暂无可用状态', '']);

function cardKind(card) {
  if (card.controls && card.controls.length > 0) return 'control';
  if (card.status && !PLACEHOLDER_STATUS.has(card.status.trim())) return 'sensor';
  return 'other';
}

function cardSubtitle(card) {
  const kind = cardKind(card);
  if (kind === 'control') {
    const names = card.controls.map(c => c.name).filter(Boolean);
    const channelSummary = names.length > 1 ? names.join(' · ') : (names[0] && names[0] !== card.name ? names[0] : '');
    const reading = card.status && !PLACEHOLDER_STATUS.has(card.status.trim()) ? card.status : '';
    return [channelSummary, reading].filter(Boolean).join(' · ') || '支持开关控制';
  }
  if (kind === 'sensor') return card.status;
  return card.status || '暂无可用状态';
}

const VIEWPORTS = [
  ['fit', '自适应', null],
  ['1024', 'ThinkSmart 8" (1024×600)', {maxWidth: '1024px', aspectRatio: '1024 / 600'}],
  ['1280', '宽屏 (1280×800)', {maxWidth: '1280px', aspectRatio: '1280 / 800'}],
];

function LiveScreen({request}) {
  const [screens, setScreens] = useState([]);
  const [selected, select] = useState('');
  const [viewport, setViewport] = useState('fit');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');

  async function refresh() {
    setBusy(true);
    setError('');
    try {
      const {screens: list = []} = await request('devices');
      setScreens(list);
      select(previous => (list.includes(previous) ? previous : list[0] || ''));
    } catch (failure) {
      setError(failure.message);
      setScreens([]);
      select('');
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { refresh(); }, []);

  const vpStyle = VIEWPORTS.find(([id]) => id === viewport)?.[2] || undefined;

  return (
    <section className="board-section">
      <div className="speaker-head-row">
        <div className="speaker-tools">
          <label className="f">
            在线音箱
            <select
              className="in"
              value={selected}
              onChange={event => select(event.target.value)}
              disabled={busy || !screens.length}
            >
              {!screens.length && <option value="">{busy ? '正在读取…' : '暂无在线音箱'}</option>}
              {screens.map(id => <option key={id}>{id}</option>)}
            </select>
          </label>
          <Button label="刷新设备" isDisabled={busy} onClick={refresh}/>
        </div>
        {selected && (
          <div className="speaker-vp-group" role="group" aria-label="预览尺寸">
            {VIEWPORTS.map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={'speaker-vp-btn' + (viewport === id ? ' on' : '')}
                onClick={() => setViewport(id)}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>
      <p className="small muted speaker-hint" role="status">
        {error || (selected
          ? '长按使用音箱麦克风，声音从音箱播放；家居控件会直接操作真实设备。'
          : busy ? '' : '请检查音箱是否已配对并连接预览。')}
      </p>
      {selected && (
        <div className="speaker-frame-wrap">
          <iframe
            title="音箱实时界面"
            referrerPolicy="no-referrer"
            style={vpStyle}
            src={'/admin/speaker/' + encodeURIComponent(selected) + '/'}
          />
        </div>
      )}
    </section>
  );
}

function LayoutEditor({request, guard}) {
  const [catalog, setCatalog] = useState({revision: null, cards: [], selected: []});
  const [selectedIds, setSelectedIds] = useState([]);
  const [query, setQuery] = useState('');
  const [filterKind, setFilterKind] = useState('all'); // 'all' | 'control' | 'sensor' | 'other'
  const [busy, setBusy] = useState(true);
  const [note, setNote] = useState('');
  const [noteError, setNoteError] = useState(false);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const cardById = useMemo(() => new Map(catalog.cards.map(c => [c.id, c])), [catalog.cards]);

  const dirty = selectedIds.length !== catalog.selected.length
    || catalog.selected.some((id, idx) => selectedIds[idx] !== id);

  useLayoutEffect(() => {
    guard.dirty = () => dirty;
    return () => { guard.dirty = null; };
  }, [dirty, guard]);

  async function load() {
    if (dirty && !confirm('放弃未保存的卡片选择与排序？')) return;
    setBusy(true);
    setNoteError(false);
    setNote('正在读取 Home Assistant 设备清单…');
    try {
      const result = await request('speaker/cards');
      setCatalog(result);
      setSelectedIds(result.selected || []);
      setNote('');
    } catch (error) {
      setNoteError(true);
      setNote(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setNoteError(false);
    setNote('正在保存布局…');
    try {
      const result = await request('speaker/cards', {revision: catalog.revision, selected: selectedIds});
      setCatalog(prev => ({...prev, revision: result.revision, selected: result.selected}));
      setSelectedIds(result.selected);
      setNote('布局已保存，音箱端将自动同步');
    } catch (error) {
      setNoteError(true);
      setNote(error.message);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); }, []);

  const counts = useMemo(() => {
    const c = {all: catalog.cards.length, control: 0, sensor: 0, other: 0};
    for (const card of catalog.cards) c[cardKind(card)] += 1;
    return c;
  }, [catalog.cards]);

  const filteredRooms = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const map = new Map();
    for (const card of catalog.cards) {
      const kind = cardKind(card);
      if (filterKind !== 'all' && kind !== filterKind) continue;
      if (needle) {
        const hay = [card.room, card.name, card.status, ...(card.controls || []).map(x => x.name)]
          .filter(Boolean).join(' ').toLowerCase();
        if (!hay.includes(needle)) continue;
      }
      if (!map.has(card.room)) map.set(card.room, []);
      map.get(card.room).push(card);
    }
    return [...map.entries()];
  }, [catalog.cards, filterKind, query]);

  function toggleCard(id) {
    setNote('');
    setSelectedIds(prev => (prev.includes(id) ? prev.filter(item => item !== id) : [...prev, id]));
  }

  function toggleRoom(cards) {
    setNote('');
    const ids = cards.map(c => c.id);
    const allChosen = ids.every(id => selectedSet.has(id));
    if (allChosen) {
      const drop = new Set(ids);
      setSelectedIds(prev => prev.filter(id => !drop.has(id)));
    } else {
      setSelectedIds(prev => {
        const existing = new Set(prev);
        return [...prev, ...ids.filter(id => !existing.has(id))];
      });
    }
  }

  function selectRecommended(includeSensors) {
    setNote('');
    const next = catalog.cards
      .filter(c => {
        const k = cardKind(c);
        return k === 'control' || (includeSensors && k === 'sensor');
      })
      .map(c => c.id);
    setSelectedIds(next);
  }

  function moveSelected(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= selectedIds.length) return;
    setNote('');
    setSelectedIds(prev => {
      const copy = [...prev];
      const [item] = copy.splice(index, 1);
      copy.splice(target, 0, item);
      return copy;
    });
  }

  const orderedSelectedCards = useMemo(
    () => selectedIds.map(id => cardById.get(id)).filter(Boolean),
    [selectedIds, cardById]
  );

  return (
    <section className="board-section ha-section">
      <header className="ha-section-head">
        <div>
          <Heading level={2}>Home Assistant 家居卡片布局</Heading>
          <p className="small muted">勾选并调整音箱智能家居面板显示的设备与顺序，不会修改 Home Assistant 权限。</p>
        </div>
        <div className="ha-head-actions">
          <Button label="重新读取" isDisabled={busy} onClick={load}/>
          <Button label="保存布局" variant="primary" isDisabled={busy || !dirty} onClick={save}/>
        </div>
      </header>

      <div className="ha-status-bar">
        <span role="status" className={noteError ? 'error-text small' : 'small muted'}>
          {note || (catalog.revision != null
            ? `已选择 ${orderedSelectedCards.length} 张卡片（共发现 ${catalog.cards.length} 台设备，可控 ${counts.control} 台，传感器 ${counts.sensor} 台）`
            : '')}
        </span>
        {dirty && <span className="pill warn">有未保存修改</span>}
      </div>

      <div className="ha-manager">
        {/* Left: Home Assistant Device Picker */}
        <div className="ha-library">
          <div className="ha-library-toolbar">
            <input
              type="search"
              className="in ha-search"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="搜索设备名、开关或房间…"
              aria-label="搜索 Home Assistant 设备"
              disabled={busy}
            />
            <div className="ha-filter-pills" role="tablist" aria-label="按设备类型筛选">
              {[
                ['all', '全部', counts.all],
                ['control', '可控制', counts.control],
                ['sensor', '传感器', counts.sensor],
                ['other', '其他', counts.other],
              ].map(([key, label, count]) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={filterKind === key}
                  className={'ha-filter-pill' + (filterKind === key ? ' on' : '')}
                  onClick={() => setFilterKind(key)}
                >
                  {label} <span>{count}</span>
                </button>
              ))}
            </div>
            <div className="ha-quick-actions">
              <button type="button" className="btn sm" disabled={busy || !counts.control} onClick={() => selectRecommended(false)}>
                仅选可控设备
              </button>
              <button type="button" className="btn sm" disabled={busy || (!counts.control && !counts.sensor)} onClick={() => selectRecommended(true)}>
                可控 + 传感器
              </button>
              <button type="button" className="btn sm" disabled={busy || !selectedIds.length} onClick={() => { setNote(''); setSelectedIds([]); }}>
                清空已选
              </button>
            </div>
          </div>

          <div className="ha-rooms-scroll">
            {!filteredRooms.length && (
              <p className="ha-empty-note muted">
                {busy ? '正在读取设备…' : catalog.cards.length ? '当前筛选条件下没有匹配的设备' : '暂未读取到 Home Assistant 设备'}
              </p>
            )}
            {filteredRooms.map(([room, cards]) => {
              const chosenInRoom = cards.filter(c => selectedSet.has(c.id)).length;
              const allChosen = cards.length > 0 && chosenInRoom === cards.length;
              return (
                <fieldset className="ha-room-group" key={room}>
                  <div className="ha-room-header">
                    <legend>
                      {room}
                      <span className="ha-room-count">已选 {chosenInRoom} / {cards.length}</span>
                    </legend>
                    <button
                      type="button"
                      className="btn sm"
                      disabled={busy}
                      onClick={() => toggleRoom(cards)}
                    >
                      {allChosen ? '取消本房间' : '全选本房间'}
                    </button>
                  </div>
                  <div className="ha-card-grid">
                    {cards.map(card => {
                      const checked = selectedSet.has(card.id);
                      const kind = cardKind(card);
                      const badge = kind === 'control'
                        ? (card.controls.length > 1 ? `可控 · ${card.controls.length}路` : '可控制')
                        : kind === 'sensor' ? '传感器' : '无状态';
                      return (
                        <label
                          key={card.id}
                          className={'ha-pick-card' + (checked ? ' selected' : '') + ' kind-' + kind}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={busy}
                            onChange={() => toggleCard(card.id)}
                          />
                          <div className="ha-pick-body">
                            <div className="ha-pick-top">
                              <strong title={card.name}>{card.name}</strong>
                              <span className={'ha-kind-badge ' + kind}>{badge}</span>
                            </div>
                            <small title={cardSubtitle(card)}>{cardSubtitle(card)}</small>
                          </div>
                        </label>
                      );
                    })}
                  </div>
                </fieldset>
              );
            })}
          </div>
        </div>

        {/* Right: Selected Cards & Display Order */}
        <aside className="ha-selected-pane" aria-label="音箱显示顺序">
          <div className="ha-selected-head">
            <h3>音箱已选卡片</h3>
            <span className="pill ok">{orderedSelectedCards.length} 张</span>
          </div>
          <p className="small muted ha-selected-tip">音箱将按以下顺序展示；可使用箭头调整卡片前后次序。</p>
          {!orderedSelectedCards.length ? (
            <div className="ha-selected-empty">
              <p>尚未勾选任何设备</p>
              <small>在左侧勾选设备，或点击「可控 + 传感器」快速挑选</small>
            </div>
          ) : (
            <ol className="ha-order-list">
              {orderedSelectedCards.map((card, index) => {
                const kind = cardKind(card);
                return (
                  <li className="ha-order-item" key={card.id}>
                    <span className="ha-order-index">{index + 1}</span>
                    <div className="ha-order-info">
                      <strong title={card.name}>{card.name}</strong>
                      <small>
                        <span className="ha-order-room">{card.room}</span>
                        <span>· {cardSubtitle(card)}</span>
                      </small>
                    </div>
                    <div className="ha-order-btns">
                      <button
                        type="button"
                        className="ha-icon-btn"
                        aria-label={`上移 ${card.name}`}
                        disabled={busy || index === 0}
                        onClick={() => moveSelected(index, -1)}
                      >↑</button>
                      <button
                        type="button"
                        className="ha-icon-btn"
                        aria-label={`下移 ${card.name}`}
                        disabled={busy || index === orderedSelectedCards.length - 1}
                        onClick={() => moveSelected(index, 1)}
                      >↓</button>
                      <button
                        type="button"
                        className="ha-icon-btn remove"
                        aria-label={`移除 ${card.name}`}
                        disabled={busy}
                        onClick={() => toggleCard(card.id)}
                      >×</button>
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
        </aside>
      </div>
    </section>
  );
}

export function mountSpeaker(host, request, guard) {
  const root = createRoot(host);
  root.render(
    <article className="speaker-console">
      <Heading level={1}>音箱预览与家居布局</Heading>
      <LiveScreen request={request}/>
      <LayoutEditor {...{request, guard}}/>
    </article>
  );
  return () => root.unmount();
}
