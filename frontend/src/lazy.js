import { defineAsyncComponent } from "vue";
import LoadingPage from "./components/LoadingPage.vue";
import ChunkError from "./components/ChunkError.vue";
export const lazy = (loader) =>
  defineAsyncComponent({
    loader,
    loadingComponent: LoadingPage,
    errorComponent: ChunkError,
    delay: 100,
    timeout: 15000,
  });
