import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import electron from 'vite-plugin-electron'
import renderer from 'vite-plugin-electron-renderer'
import { resolve } from 'path'

export default defineConfig({
  // 合并版：静态资源挂在 /ls/ 下（web/ls/），所以 base 必须一起改
  base: '/ls/',
  plugins: [
    vue(),
    electron([
      {
        entry: 'electron/main.ts',
        vite: {
          build: {
            outDir: 'dist-electron',
            rollupOptions: {
              external: ['electron'],
            },
          },
        },
      },
      {
        entry: 'electron/preload.ts',
        onstart(options) {
          options.reload()
        },
        vite: {
          build: {
            outDir: 'dist-electron',
            rollupOptions: {
              external: ['electron'],
            },
          },
        },
      },
    ]),
    renderer(),
  ],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    // 开发模式：直接代理到合并版 LiveTalking（8010），不再是独立的 8020
    proxy: {
      '/ls/api': `http://localhost:${process.env.BACKEND_PORT || '8010'}`,
      '/ls/ws': {
        target: `ws://localhost:${process.env.BACKEND_PORT || '8010'}`,
        ws: true,
      },
    },
  },
})
