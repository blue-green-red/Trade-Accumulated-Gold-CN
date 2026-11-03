import { onActivated, onDeactivated, onUnmounted } from "vue";
export function usePage(load, milliseconds = 30000) {
  let timer;
  const refresh = () => load(true);
  const visible = () => {
    if (!document.hidden) load();
  };
  function stop() {
    clearInterval(timer);
    window.removeEventListener("public-refresh", refresh);
    document.removeEventListener("visibilitychange", visible);
  }
  onActivated(() => {
    stop();
    load();
    timer = setInterval(() => {
      if (!document.hidden) load();
    }, milliseconds);
    window.addEventListener("public-refresh", refresh);
    document.addEventListener("visibilitychange", visible);
  });
  onDeactivated(stop);
  onUnmounted(stop);
}
