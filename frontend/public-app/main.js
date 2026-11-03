import { createApp } from "vue";
import App from "./App.vue";
import "./theme.css";
import "./style.css";
function applyTheme() {
  let theme = "auto";
  try {
    theme = localStorage.getItem("gold-theme") || "auto";
  } catch {}
  if (theme === "light" || theme === "dark")
    document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
applyTheme();
window.addEventListener("storage", (event) => {
  if (event.key === "gold-theme") applyTheme();
});
createApp(App).mount("#app");
