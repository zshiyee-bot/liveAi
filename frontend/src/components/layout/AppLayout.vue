<template>
  <el-container style="height: 100vh">
    <el-aside width="200px" style="background: #1d1e1f; color: #fff">
      <SideNav />
    </el-aside>
    <el-container>
      <el-main style="background: #f5f7fa; padding: 20px">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import SideNav from './SideNav.vue'
</script>
