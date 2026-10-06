import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import "@vue-flow/core/dist/style.css";
import "@vue-flow/core/dist/theme-default.css";
import "@vue-flow/minimap/dist/style.css";
import "./styles.css";

createApp(App).use(createPinia()).mount("#app");
