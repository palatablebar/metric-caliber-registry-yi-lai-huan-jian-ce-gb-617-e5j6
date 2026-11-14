import { api } from '../api.js';
import { ENUMS, badgeTone, can } from '../config.js';
import { chips, drawer, emptyState, errorBox, errorMessage, esc, field, filterBar, fmtTime, impactRows, kv, table, toast } from '../ui.js';

const state = { severity: '', kind: '' };

function variantCard(metric, index) {
  return `<div class="diff-col">
    <h4>口径 ${String(index + 1).padStart(2, '0')} · ${esc(metric.code)}</h4>
    <p class="section-note">${esc(metric.expression || '—')}</p>
    <div class="dsl-box"><span class="label">字段</span>${chips(metric.fields, { withIndex: false })}</div>
    <div class="dsl-box"><span class="label">过滤</span>${chips(metric.filters, { withIndex: false })}</div>
    <div class="dsl-box"><span class="label">聚合</span>${chips(metric.aggs, { withIndex: false })}</div>
  </div>`;
}

function openConflict(view, ctx, conflict) {
  const writable = can(ctx.user.role, 'change:write');
  const mergeWritable = can(ctx.user.role, 'merge:write');
  const first = conflict.metrics[0];
  const survivorOptions = conflict.metrics
    .map((item) => `<option value="${esc(item.code)}">${esc(item.code)} · ${esc(item.expression || '—')}</option>`)
    .join('');
  const { root, close } = drawer({
    title: `同名冲突 · ${conflict.name}`,
    subtitle: `${conflict.metric_codes.join(' vs ')} · ${ENUMS.conflictKind[conflict.kind] || conflict.kind}`,
    body: `
      ${kv([
        ['差异类型', esc(ENUMS.conflictKind[conflict.kind] || conflict.kind)],
        ['严重度', `<span class="badge ${badgeTone(conflict.severity)}">${esc(ENUMS.severity[conflict.severity])}</span>`],
        ['影响分', `<span class="mono">${esc(conflict.score)}</span>`],
        ['下游报表', `${esc(conflict.impact.report_count)} 张（核心 ${esc(conflict.impact.core_count)} 张）`],
      ])}
      <div class="diff-cols">${conflict.metrics.map(variantCard).join('')}</div>
      <div class="dsl-box">
        <span class="label">差异明细</span>
        <div class="list-tight">
          <div class="impact-row"><span>仅 ${esc(conflict.metrics[0].code)} 有的字段</span><span class="mono">${esc((conflict.diff.fields_only_first || []).join(' / ') || '—')}</span></div>
          <div class="impact-row"><span>仅其他口径有的字段</span><span class="mono">${esc((conflict.diff.fields_only_others || []).join(' / ') || '—')}</span></div>
          <div class="impact-row"><span>仅 ${esc(conflict.metrics[0].code)} 的过滤条件</span><span class="mono">${esc((conflict.diff.filters_only_first || []).join(' ; ') || '—')}</span></div>
          <div class="impact-row"><span>仅其他口径的过滤条件</span><span class="mono">${esc((conflict.diff.filters_only_others || []).join(' ; ') || '—')}</span></div>
        </div>
      </div>
      <div class="dsl-box">
        <span class="label">影响面 · 下游报表（核心报表权重 ×3）</span>
        ${impactRows(conflict.impact)}
      </div>
      <div class="dsl-box">
        <span class="label">建议动作</span>
        <p class="section-note">${esc(conflict.suggestion)}</p>
      </div>
      ${
        writable
          ? `<div class="dsl-box">
              <span class="label">把这份冲突变成变更单</span>
              ${field({ label: '变更指标', name: 'metric_code', value: first.code, options: conflict.metrics.map((item) => ({ value: item.code, label: `${item.code} · ${item.name}` })) })}
              ${field({ label: '原因（月会结论 / 数仓变更）', name: 'reason', value: `同名冲突「${conflict.name}」：${conflict.suggestion}` })}
              <button class="btn sm accent" id="create-cr" type="button">提交变更单</button>
            </div>`
          : '<p class="muted tiny">只读角色可以查看冲突与影响面，提交变更单需要专员及以上角色。</p>'
      }
      ${
        mergeWritable
          ? `<div class="dsl-box" id="merge-box">
              <span class="label">业务选定保留口径 → 合并同名口径</span>
              <p class="section-note">被合并指标的版本、口径历史、依赖来源与报表引用会完整归档；引用改指保留口径，报表按 code 去重。合并会产生自引用或依赖环时整次操作不做任何写入。</p>
              <div class="field"><label for="merge-survivor">保留口径</label>
                <select id="merge-survivor" name="survivor_code">${survivorOptions}</select></div>
              <button class="btn sm ghost" id="merge-preview" type="button">预览迁移计划</button>
              <div id="merge-result"></div>
            </div>`
          : ''
      }`,
  });

  root.querySelector('#create-cr')?.addEventListener('click', async () => {
    const metricCode = root.querySelector('[name=metric_code]')?.value || first.code;
    const reason = root.querySelector('[name=reason]')?.value || '';
    try {
      await api.post('/api/change-requests', {
        change_kind: 'caliber',
        target_ref: metricCode,
        metric_code: metricCode,
        reason,
      });
      toast('变更单已提交，等待管理员审批');
      close();
      await conflictsPage.render(view, ctx);
    } catch (err) {
      toast(err.code === 'CONFLICT' ? '该指标已有待审批的同类变更单' : errorMessage(err), 'err');
    }
  });

  if (!mergeWritable) return;
  const survivorSelect = root.querySelector('#merge-survivor');
  const resultBox = root.querySelector('#merge-result');
  const payloadOf = () => {
    const survivor = survivorSelect.value;
    return {
      survivor_code: survivor,
      source_codes: conflict.metric_codes.filter((code) => code !== survivor),
    };
  };
  const renderPlan = (plan) => {
    if (plan.kind === 'already_merged') {
      resultBox.innerHTML = `<p class="section-note">这组同名口径已经合并过（记录 #${esc(plan.existing_merge_id)}），重复提交不会再次迁移、不会生成新版本或审计记录。</p>`;
      return;
    }
    const impact = plan.impact || {};
    const blockers = plan.blockers || [];
    const migratedReports = plan.reports_migrated || [];
    const migratedRefs = plan.references_migrated || [];
    resultBox.innerHTML = `
      ${
        blockers.length
          ? `<div class="error-box"><span>合并被阻止：${esc(blockers.map((item) => item.message).join('；'))}</span></div>
             <div class="list-tight">${blockers
               .map(
                 (item) =>
                   `<div class="impact-row"><span class="mono">${esc((item.path || []).join(' → '))}</span><span class="badge danger">${esc(item.kind === 'self_reference' ? '自引用' : '依赖环')}</span></div>`,
               )
               .join('')}</div>`
          : `<div class="impact-row"><span>保留口径</span><span class="mono">${esc(plan.survivor_code)}</span></div>
             <div class="impact-row"><span>被合并口径</span><span class="mono">${esc((plan.source_codes || []).join(' / '))}</span></div>
             <div class="impact-row"><span>迁移报表数</span><span class="mono">${esc(plan.reports_migrated_count || 0)}</span></div>
             <div class="impact-row"><span>迁移引用（指标）</span><span class="mono">${esc(migratedRefs.length)}</span></div>
             <div class="impact-row"><span>影响分（去重后）</span><span class="mono">${esc(impact.score_before || 0)} → ${esc(impact.score || 0)}</span></div>
             <div class="impact-row"><span>下游报表（去重后）</span><span class="mono">${esc(impact.report_count_before || 0)} → ${esc(impact.report_count || 0)} 张</span></div>
             ${
               migratedReports.length
                 ? `<div class="list-tight">${migratedReports
                     .map(
                       (report) =>
                         `<div class="impact-row"><span class="mono">${esc(report.code)}</span><span class="tiny">${esc(report.before.join(' / '))} → ${esc(report.after.join(' / '))}</span></div>`,
                     )
                     .join('')}</div>`
                 : ''
             }
             <button class="btn sm accent" id="merge-execute" type="button">执行合并</button>`
      }`;
    resultBox.querySelector('#merge-execute')?.addEventListener('click', async () => {
      try {
        const result = await api.post('/api/merges', payloadOf());
        toast(
          result.duplicate
            ? '这组口径已合并过，未重复生成版本或审计记录'
            : `已合并：迁移 ${result.summary?.reports_migrated_count || 0} 张报表、${result.summary?.references_migrated_count || 0} 条引用`,
          result.duplicate ? '' : 'ok',
        );
        close();
        await conflictsPage.render(view, ctx);
      } catch (err) {
        const blockers = err.detail?.blockers;
        if (err.code === 'MERGE_BLOCKED' && blockers) {
          resultBox.innerHTML = `<div class="error-box"><span>合并会产生自引用或依赖环，整次操作已保留原样：${esc(
            (err.detail.conflict_paths || []).join('；'),
          )}</span></div>`;
        } else {
          toast(errorMessage(err), 'err');
        }
      }
    });
  };
  root.querySelector('#merge-preview')?.addEventListener('click', async () => {
    try {
      const plan = await api.post('/api/merges/preview', payloadOf());
      renderPlan(plan);
    } catch (err) {
      resultBox.innerHTML = `<div class="error-box"><span>${esc(errorMessage(err))}</span></div>`;
    }
  });
  survivorSelect?.addEventListener('change', () => {
    resultBox.innerHTML = '<p class="muted tiny">口径选择已变化，请重新预览迁移计划。</p>';
  });
}

function openMerge(ctx, merge) {
  const summary = merge.summary || {};
  const archived = merge.archived || {};
  const sources = archived.sources || [];
  const impact = summary.impact || {};
  drawer({
    title: `合并归档 · ${merge.survivor_code}`,
    subtitle: `${merge.merge_key} · ${fmtTime(merge.created_at)}`,
    body: `
      ${kv([
        ['保留口径', `<span class="mono">${esc(merge.survivor_code)} · ${esc(merge.survivor_name)}</span>`],
        ['被合并口径', `<span class="mono">${esc(merge.source_code)}</span>`],
        ['迁移报表', `${esc(summary.reports_migrated_count || 0)} 张（${esc((summary.reports_migrated || []).join(' / ') || '—')}）`],
        ['迁移引用', `${esc(summary.references_migrated_count || 0)} 条 · 新建版本 ${esc((summary.versions_created || []).length)} 个`],
        ['影响分', `<span class="mono">${esc(impact.score_before ?? 0)} → ${esc(impact.score ?? 0)}</span>（去重后）`],
        ['操作人', esc(merge.requested_by)],
      ])}
      ${sources
        .map(
          (source) => `<div class="dsl-box">
            <span class="label">归档 · ${esc(source.metric.code)}（${esc(source.metric.status)}）</span>
            <div class="list-tight">
              ${(source.formulas || [])
                .map(
                  (formula) =>
                    `<div class="impact-row"><span class="mono">v${esc(formula.version)}</span><span class="tiny">${esc(formula.dsl_text)}</span></div>`,
                )
                .join('')}
              <div class="impact-row"><span>依赖来源</span><span class="mono tiny">${esc(
                (source.dependencies || []).map((edge) => `${edge.from_metric_code || '—'} →${edge.kind === 'metric' ? '' : '[' + edge.kind + ']'} ${edge.to_metric_code || edge.to_ref}`).join(' ; ') || '—',
              )}</span></div>
              <div class="impact-row"><span>报表引用（合并前）</span><span class="mono tiny">${esc((source.reports || []).map((report) => report.code).join(' / ') || '—')}</span></div>
            </div>
          </div>`,
        )
        .join('')}
      <div class="dsl-box">
        <span class="label">报表引用迁移（before → after）</span>
        <div class="list-tight">${(archived.report_migrations || [])
          .map(
            (item) =>
              `<div class="impact-row"><span class="mono">${esc(item.code)}</span><span class="tiny">${esc(item.before.join(' / '))} → ${esc(item.after.join(' / '))}</span></div>`,
          )
          .join('') || '<p class="muted tiny">没有报表引用需要迁移。</p>'}</div>
      </div>`,
  });
}

export const conflictsPage = {
  async render(view, ctx) {
    let data;
    let merges;
    try {
      data = await api.get('/api/conflicts', { severity: state.severity, kind: state.kind, limit: 50 });
      merges = await api.get('/api/merges', { limit: 20 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => conflictsPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>同名不同口径 · 冲突与影响面</h2>
          <div style="display:flex;gap:10px;align-items:center">
            <span class="meta">${data.total} 处</span>
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost" id="export-conflicts">导出 CSV</button>' : ''}
          </div>
        </div>
        ${filterBar('conflict-filters', [
          { label: '严重度', name: 'severity', value: state.severity, options: [{ value: '', label: '全部等级' }, ...Object.entries(ENUMS.severity).map(([value, label]) => ({ value, label }))] },
          { label: '差异类型', name: 'kind', value: state.kind, options: [{ value: '', label: '全部类型' }, ...Object.entries(ENUMS.conflictKind).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: data.items,
          columns: [
            { key: 'name', label: '同名指标', render: (row) => `<b>${esc(row.name)}</b>` },
            {
              key: 'kind',
              label: '差异类型',
              render: (row) => `<span class="badge warn">${esc(ENUMS.conflictKind[row.kind] || row.kind)}</span>`,
            },
            {
              key: 'severity',
              label: '严重度',
              render: (row) => `<span class="badge ${badgeTone(row.severity)}">${esc(ENUMS.severity[row.severity])}</span>`,
            },
            { key: 'metric_codes', label: '口径成员', className: 'mono', render: (row) => esc(row.metric_codes.join(' / ')) },
            { key: 'score', label: '影响分', className: 'num' },
            { key: 'impact', label: '下游报表', className: 'num', render: (row) => esc(row.impact.report_count) },
            { key: 'suggestion', label: '建议动作', render: (row) => `<span class="tiny">${esc(row.suggestion.slice(0, 42))}…</span>` },
          ],
          empty: { title: '没有匹配的冲突', hint: '调整筛选条件，或改一条口径后再跑校验。' },
        })}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>合并归档</h2><span class="meta">${merges.total} 次 · 版本与依赖来源完整留档</span></div>
        ${table({
          rows: merges.items,
          columns: [
            { key: 'survivor_code', label: '保留口径', className: 'mono' },
            { key: 'source_code', label: '被合并口径', className: 'mono' },
            { key: 'summary', label: '迁移报表', className: 'num', render: (row) => esc(row.summary?.reports_migrated_count ?? 0) },
            { key: 'summary2', label: '迁移引用', className: 'num', render: (row) => esc(row.summary?.references_migrated_count ?? 0) },
            { key: 'impact', label: '影响分', className: 'num', render: (row) => esc(row.summary?.impact?.score ?? 0) },
            { key: 'created_at', label: '合并时间', render: (row) => `<span class="tiny">${fmtTime(row.created_at)}</span>` },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ok">${esc(ENUMS.mergeStatus[row.status] || row.status)}</span>` },
          ],
          empty: { title: '还没有合并记录', hint: '在任一冲突抽屉里选定保留口径并执行合并。' },
        })}
      </section>`;

    const filters = view.querySelector('#conflict-filters');
    filters?.addEventListener('change', () => {
      state.severity = filters.querySelector('[name=severity]').value;
      state.kind = filters.querySelector('[name=kind]').value;
      conflictsPage.render(view, ctx);
    });
    view.querySelector('#export-conflicts')?.addEventListener('click', async () => {
      try {
        await api.download('/api/exports/conflicts.csv', 'conflicts.csv');
        toast('冲突与影响面 CSV 已下载');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    view.querySelectorAll('section.panel')[0]?.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const name = row.querySelector('b')?.textContent;
        const item = data.items.find((entry) => entry.name === name);
        if (item) openConflict(view, ctx, item);
      });
    });
    view.querySelectorAll('section.panel')[1]?.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const code = row.querySelector('td.mono')?.textContent?.trim();
        const item = merges.items.find((entry) => entry.survivor_code === code);
        if (item) openMerge(ctx, item);
      });
    });
  },
};

export default conflictsPage;
