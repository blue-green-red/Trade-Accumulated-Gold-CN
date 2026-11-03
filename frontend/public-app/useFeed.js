import { ref } from "vue";
import { feeds } from "./cache.js";
import { usePage } from "./usePage.js";
export function useFeed(url, ttl = 30000) {
  const data = ref(feeds.peek(url) || null),
    error = ref(false),
    loading = ref(false);
  async function load(force = false) {
    loading.value = true;
    error.value = false;
    try {
      data.value = await feeds.read(url, { ttl, force });
    } catch {
      error.value = true;
    } finally {
      loading.value = false;
    }
  }
  usePage(load, ttl);
  return { data, error, loading, load };
}
