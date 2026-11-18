import { initVault } from './vault.js';
import { initCliLogin } from './cli-login.js';
import { initAccount } from './account.js';
import { initManage } from './manage.js';
import { initFiles } from './files.js';
import { initTopics } from './topics.js';
import { initChat } from './chat.js';
import { loadI18n } from './i18n.js';
import { initTheme } from './theme.js';
import { initRouter, renderRoute } from './router.js';
import { initAuth, notify } from './auth.js';

async function start() {
  try {
    await loadI18n();
    initTheme();
    initRouter();
    initTopics();
    initChat();
    initManage();
    initFiles();
    initCliLogin();
    initAccount();
    initVault();
    await initAuth();
  } catch { notify('error.bot_unreachable'); }
  finally {
    renderRoute();
    document.body.removeAttribute('data-auth-pending');
  }
}
start();
