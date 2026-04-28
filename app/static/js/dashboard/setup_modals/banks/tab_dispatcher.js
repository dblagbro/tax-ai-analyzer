/* setup_modals/banks/tab_dispatcher.js — Import Hub tab → status hook
 *
 * Phase 11E refactor: extracted verbatim from setup_modals.js (last
 * portion of the combined banks IIFE, lines 1118-1141 of the original).
 * Loads LAST among setup_modals/* scripts so that all `loadXxxStatus`
 * functions installed by the per-bank IIFEs are guaranteed to exist
 * before this wrapper captures them.
 */
(function() {
  // Hook impTab so switching to a per-source sub-tab triggers a status refresh
  var _origImpTab2 = window.impTab;
  window.impTab = function(name, btn) {
    if (_origImpTab2) _origImpTab2(name, btn);
    if (name === 'capitalone') loadCoStatus();
    if (name === 'usbank')     loadUsbStatus();
    if (name === 'merrick')    loadMrkStatus();
    if (name === 'chime')      loadChimeStatus();
    if (name === 'verizon')    loadVznStatus();
    if (name === 'simplefin')  loadSfinStatus();
    if (name === 'plaid')      loadPlaidStatus();
    if (name === 'imap')       loadImapStatus();
  };

  // Also load status on page init for Import tab — pre-populate badges
  // when the Import tab is the initial active tab on page load.
  document.addEventListener('DOMContentLoaded', function() {
    if (document.getElementById('ip-capitalone')) loadCoStatus();
    if (document.getElementById('sfin-status-badge')) loadSfinStatus();
    if (document.getElementById('chm-status')) loadChimeStatus();
    if (document.getElementById('vzn-status')) loadVznStatus();
    if (document.getElementById('plaid-status-badge')) loadPlaidStatus();
  });
})();
