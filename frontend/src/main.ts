import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import MobileApp from './MobileApp.vue'

const route = window.location.pathname.replace(/\/+$/, '') || '/'

createApp(route === '/mobile' ? MobileApp : App).mount('#app')
