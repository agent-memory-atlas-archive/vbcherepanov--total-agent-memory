'use strict';
window.TamSections.memory = async (container, ctx) => {
  const reply = await ctx.api('memory', {method: 'POST', body: {name: 'memory_scopes', arguments: {}}});
  const teamNames = Object.fromEntries(ctx.teams.map((t) => [t.team_id, t.name]));
  container.append(ctx.ui.h('p', {class: 'muted', text: 'Your personal memory is private: nobody else can read it here, administrators included.'}));
  window.TamDashboard.recordBrowser(container, ctx, {scopes: reply.workspaces, allowAll: true, teamNames});
};
