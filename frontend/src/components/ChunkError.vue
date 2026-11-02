<script setup>
const reload = () => location.reload();
</script>
<template>
  <div class="notice warn" role="alert">
    页面代码加载失败，请检查网络后重试。<button
      class="link-btn"
      @click="reload"
    >
      重新加载页面 →
    </button>
  </div>
</template>
