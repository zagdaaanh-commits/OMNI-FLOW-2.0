from __future__ import annotations


def get_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>OmniFlow AI | Multi-Agent Social Marketing Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" />
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: {
              50: '#f0fdf4',
              100: '#dcfce7',
              500: '#22c55e',
              600: '#16a34a',
              700: '#15803d',
              900: '#14532d',
            }
          }
        }
      }
    }
  </script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');
    body { font-family: 'Inter', sans-serif; }
    .glass { background: rgba(255, 255, 255, 0.85); backdrop-filter: blur(12px); }
    .tab-active { border-bottom: 2px solid #2563eb; color: #2563eb; font-weight: 600; }
  </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen flex flex-col">
  <!-- Top Navigation -->
  <header class="sticky top-0 z-40 bg-white border-b border-slate-200 shadow-sm">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex justify-between items-center h-16">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 to-indigo-600 flex items-center justify-center text-white shadow-md shadow-blue-500/30">
          <i class="fa-solid fa-bolt-lightning text-lg"></i>
        </div>
        <div>
          <span class="text-xl font-extrabold bg-gradient-to-r from-blue-700 via-indigo-600 to-purple-600 bg-clip-text text-transparent">OmniFlow AI</span>
          <span class="hidden md:inline-block ml-2 px-2 py-0.5 text-xs font-semibold rounded bg-blue-50 text-blue-700 border border-blue-200">Cross-Border & Domestic</span>
        </div>
      </div>

      <div class="flex items-center space-x-3">
        <div class="hidden sm:flex items-center space-x-2 text-xs text-slate-500 bg-slate-100 px-3 py-1.5 rounded-full border border-slate-200">
          <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
          <span>FastAPI Engine (Port 5000)</span>
        </div>
        <button onclick="seedDemoData()" id="seedBtn" class="inline-flex items-center space-x-1.5 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-700 hover:to-teal-700 text-white text-xs font-semibold px-3 py-2 rounded-lg shadow-sm transition">
          <i class="fa-solid fa-wand-magic-sparkles"></i>
          <span>Instant Demo Seed</span>
        </button>
        <a href="/docs" target="_blank" class="inline-flex items-center space-x-1 bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium px-3 py-2 rounded-lg border border-slate-300 transition">
          <i class="fa-solid fa-code"></i>
          <span>Swagger Docs</span>
        </a>
      </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex space-x-8 text-sm overflow-x-auto">
      <button onclick="switchTab('analytics')" id="tab-analytics" class="py-3 px-1 border-b-2 border-transparent text-slate-500 hover:text-slate-700 tab-active flex items-center space-x-2">
        <i class="fa-solid fa-chart-line"></i>
        <span>Analytics & ROI</span>
      </button>
      <button onclick="switchTab('campaigns')" id="tab-campaigns" class="py-3 px-1 border-b-2 border-transparent text-slate-500 hover:text-slate-700 flex items-center space-x-2">
        <i class="fa-solid fa-bullhorn"></i>
        <span>Campaigns</span>
      </button>
      <button onclick="switchTab('studio')" id="tab-studio" class="py-3 px-1 border-b-2 border-transparent text-slate-500 hover:text-slate-700 flex items-center space-x-2">
        <i class="fa-solid fa-pen-nib"></i>
        <span>Content Studio</span>
      </button>
      <button onclick="switchTab('schedules')" id="tab-schedules" class="py-3 px-1 border-b-2 border-transparent text-slate-500 hover:text-slate-700 flex items-center space-x-2">
        <i class="fa-solid fa-calendar-check"></i>
        <span>Publishing & Tasks</span>
      </button>
    </div>
  </header>

  <!-- Notification Toast -->
  <div id="toast" class="fixed top-20 right-5 z-50 transform transition-all duration-300 translate-y-[-20px] opacity-0 pointer-events-none bg-slate-900 text-white text-sm px-4 py-3 rounded-xl shadow-xl flex items-center space-x-3">
    <i id="toastIcon" class="fa-solid fa-circle-check text-emerald-400 text-base"></i>
    <span id="toastMsg">Operation completed successfully</span>
  </div>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-1 w-full">

    <!-- ==================== TAB 1: ANALYTICS & ROI ==================== -->
    <section id="section-analytics" class="space-y-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-slate-900">Campaign Analytics & Performance</h1>
          <p class="text-sm text-slate-500">Cross-platform metrics aggregation: Meta, Instagram, TikTok, Xiaohongshu, WeChat, Douyin, and X.</p>
        </div>
        <div class="flex items-center space-x-2">
          <select id="periodSelect" onchange="loadAnalytics()" class="text-sm bg-white border border-slate-300 rounded-lg px-3 py-2 text-slate-700 focus:ring-2 focus:ring-blue-500 outline-none">
            <option value="7">Last 7 Days</option>
            <option value="30" selected>Last 30 Days</option>
            <option value="90">Last 90 Days</option>
          </select>
          <button onclick="loadAnalytics()" class="bg-white border border-slate-300 hover:bg-slate-50 text-slate-700 px-3 py-2 rounded-lg text-sm transition">
            <i class="fa-solid fa-arrow-rotate-right"></i>
          </button>
        </div>
      </div>

      <!-- KPI Summary Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm">
          <div class="flex justify-between items-start">
            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Total Impressions</span>
            <span class="p-2 rounded-xl bg-blue-50 text-blue-600"><i class="fa-solid fa-eye"></i></span>
          </div>
          <div class="mt-2 flex items-baseline justify-between">
            <span id="metric-impressions" class="text-2xl font-bold text-slate-900">415,000</span>
            <span class="text-xs font-semibold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full">+18.4%</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">Across all active channels</p>
        </div>

        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm">
          <div class="flex justify-between items-start">
            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Total Clicks</span>
            <span class="p-2 rounded-xl bg-indigo-50 text-indigo-600"><i class="fa-solid fa-arrow-pointer"></i></span>
          </div>
          <div class="mt-2 flex items-baseline justify-between">
            <span id="metric-clicks" class="text-2xl font-bold text-slate-900">18,770</span>
            <span id="metric-ctr" class="text-xs font-semibold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded-full">4.52% CTR</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">High conversion intent</p>
        </div>

        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm">
          <div class="flex justify-between items-start">
            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Total Spend</span>
            <span class="p-2 rounded-xl bg-amber-50 text-amber-600"><i class="fa-solid fa-dollar-sign"></i></span>
          </div>
          <div class="mt-2 flex items-baseline justify-between">
            <span id="metric-spend" class="text-2xl font-bold text-slate-900">$910.00</span>
            <span class="text-xs font-medium text-slate-500">USD</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">Optimized across channels</p>
        </div>

        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm">
          <div class="flex justify-between items-start">
            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Est. Revenue</span>
            <span class="p-2 rounded-xl bg-emerald-50 text-emerald-600"><i class="fa-solid fa-sack-dollar"></i></span>
          </div>
          <div class="mt-2 flex items-baseline justify-between">
            <span id="metric-revenue" class="text-2xl font-bold text-emerald-600">$3,094.00</span>
            <span class="text-xs font-semibold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-full">3.4x</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">Attributed sales volume</p>
        </div>

        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm col-span-2 lg:col-span-1">
          <div class="flex justify-between items-start">
            <span class="text-xs font-semibold text-slate-500 uppercase tracking-wider">Return on Ad Spend (ROI)</span>
            <span class="p-2 rounded-xl bg-purple-50 text-purple-600"><i class="fa-solid fa-rocket"></i></span>
          </div>
          <div class="mt-2 flex items-baseline justify-between">
            <span id="metric-roi" class="text-2xl font-extrabold text-purple-700">+240.0%</span>
            <span class="text-xs font-semibold text-purple-600 bg-purple-50 px-2 py-0.5 rounded-full">Top Tier</span>
          </div>
          <p class="text-xs text-slate-400 mt-1">Net marketing return</p>
        </div>
      </div>

      <!-- Charts Section -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm lg:col-span-2">
          <h2 class="text-base font-bold text-slate-900 mb-1">Platform Impressions & Views Comparison</h2>
          <p class="text-xs text-slate-500 mb-4">Volume breakdown across international and domestic Asia social networks</p>
          <div class="h-72">
            <canvas id="channelChart"></canvas>
          </div>
        </div>

        <div class="bg-white p-5 rounded-2xl border border-slate-200 shadow-sm">
          <h2 class="text-base font-bold text-slate-900 mb-1">Engagement & Clicks Share</h2>
          <p class="text-xs text-slate-500 mb-4">Proportion of user interactions</p>
          <div class="h-72 flex items-center justify-center">
            <canvas id="engagementChart"></canvas>
          </div>
        </div>
      </div>

      <!-- Recommendations & Anomaly Banner -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div class="bg-gradient-to-br from-blue-50 to-indigo-50 border border-blue-200 rounded-2xl p-5">
          <div class="flex items-center space-x-2 text-blue-800 font-bold text-sm mb-3">
            <i class="fa-solid fa-lightbulb text-amber-500"></i>
            <span>AI Optimization Recommendations</span>
          </div>
          <ul id="recommendationsList" class="space-y-2 text-xs text-slate-700">
            <li class="flex items-start space-x-2">
              <span class="text-blue-600 font-bold">•</span>
              <span>Prioritize TikTok and Xiaohongshu for high-velocity visual demonstrations.</span>
            </li>
            <li class="flex items-start space-x-2">
              <span class="text-blue-600 font-bold">•</span>
              <span>Reallocate 20% budget from low-CTR channels to short-video formats.</span>
            </li>
            <li class="flex items-start space-x-2">
              <span class="text-blue-600 font-bold">•</span>
              <span>Schedule domestic Asia posts between 19:00 - 21:30 local time for organic lift.</span>
            </li>
          </ul>
        </div>

        <div class="bg-gradient-to-br from-emerald-50 to-teal-50 border border-emerald-200 rounded-2xl p-5">
          <div class="flex items-center space-x-2 text-emerald-900 font-bold text-sm mb-3">
            <i class="fa-solid fa-shield-check text-emerald-600"></i>
            <span>System Health & Channel Diagnostics</span>
          </div>
          <ul id="anomaliesList" class="space-y-2 text-xs text-slate-700">
            <li class="flex items-start space-x-2">
              <span class="text-emerald-600 font-bold">•</span>
              <span>All 7 channel publisher pipelines operating normally with 100% execution reliability.</span>
            </li>
            <li class="flex items-start space-x-2">
              <span class="text-emerald-600 font-bold">•</span>
              <span>Xiaohongshu engagement rate peaked at 9.1% with strong bookmarking intent.</span>
            </li>
            <li class="flex items-start space-x-2">
              <span class="text-emerald-600 font-bold">•</span>
              <span>Scheduler worker active: automated background publish jobs ready.</span>
            </li>
          </ul>
        </div>
      </div>
    </section>

    <!-- ==================== TAB 2: CAMPAIGN MANAGER ==================== -->
    <section id="section-campaigns" class="hidden space-y-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-slate-900">Campaign Management</h1>
          <p class="text-sm text-slate-500">Plan multi-channel marketing campaigns using CampaignPlanner agent.</p>
        </div>
        <button onclick="toggleCampaignForm()" class="inline-flex items-center space-x-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold px-4 py-2 rounded-xl shadow transition">
          <i class="fa-solid fa-plus"></i>
          <span>Create New Campaign</span>
        </button>
      </div>

      <!-- Create Campaign Drawer / Form -->
      <div id="campaignFormContainer" class="hidden bg-white border border-slate-200 rounded-2xl p-6 shadow-md transition-all">
        <div class="flex justify-between items-center mb-4">
          <h2 class="text-lg font-bold text-slate-900">Create New Campaign</h2>
          <div class="flex items-center space-x-2 text-xs">
            <span class="text-slate-500">Quick Presets:</span>
            <button onclick="prefillPreset('fashion')" class="px-2 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded border">Fashion & Gifts</button>
            <button onclick="prefillPreset('saas')" class="px-2 py-1 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded border">Tech & Gadgets</button>
          </div>
        </div>

        <form id="createCampaignForm" onsubmit="handleCreateCampaign(event)" class="space-y-4">
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label class="block text-xs font-semibold text-slate-700 mb-1">Campaign Name *</label>
              <input type="text" id="campName" required value="Summer Cross-Border Growth" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-semibold text-slate-700 mb-1">Objective *</label>
              <input type="text" id="campObjective" required value="Increase product awareness and conversion for premium summer gifts" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <label class="block text-xs font-semibold text-slate-700 mb-1">Total Budget (USD)</label>
              <input type="number" id="campBudget" value="1200" min="0" step="50" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-semibold text-slate-700 mb-1">Brand Voice</label>
              <input type="text" id="campVoice" value="friendly, premium, modern, authentic" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label class="block text-xs font-semibold text-slate-700 mb-1">Posting Frequency / Day</label>
              <input type="number" id="campFrequency" value="2" min="1" max="10" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
          </div>

          <div>
            <label class="block text-xs font-semibold text-slate-700 mb-1">Product Description</label>
            <textarea id="campProduct" rows="2" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500">Handcrafted leather gifts and contemporary accessories for international and domestic lifestyle lovers.</textarea>
          </div>

          <div>
            <label class="block text-xs font-semibold text-slate-700 mb-1">Target Platforms</label>
            <div class="flex flex-wrap gap-2 text-xs">
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="meta" checked class="text-blue-600 rounded">
                <span>Meta/Facebook</span>
              </label>
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="instagram" checked class="text-blue-600 rounded">
                <span>Instagram</span>
              </label>
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="tiktok" checked class="text-blue-600 rounded">
                <span>TikTok</span>
              </label>
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="xiaohongshu" checked class="text-blue-600 rounded">
                <span>Xiaohongshu (RED)</span>
              </label>
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="douyin" checked class="text-blue-600 rounded">
                <span>Douyin</span>
              </label>
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="wechat" checked class="text-blue-600 rounded">
                <span>WeChat</span>
              </label>
              <label class="inline-flex items-center space-x-1.5 bg-slate-100 px-3 py-1.5 rounded-lg cursor-pointer hover:bg-slate-200">
                <input type="checkbox" name="platformCheck" value="x" checked class="text-blue-600 rounded">
                <span>X / Twitter</span>
              </label>
            </div>
          </div>

          <div class="flex justify-end space-x-3 pt-2">
            <button type="button" onclick="toggleCampaignForm()" class="px-4 py-2 border border-slate-300 text-slate-700 rounded-lg text-sm hover:bg-slate-50">Cancel</button>
            <button type="submit" id="campSubmitBtn" class="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg text-sm shadow">
              <span>Build Strategy & Save</span>
            </button>
          </div>
        </form>
      </div>

      <!-- Campaign List Grid -->
      <div id="campaignList" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <!-- Dynamically rendered -->
      </div>
    </section>

    <!-- ==================== TAB 3: CONTENT STUDIO ==================== -->
    <section id="section-studio" class="hidden space-y-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-slate-900">Multi-Agent Content Studio</h1>
          <p class="text-sm text-slate-500">Generate platform-tailored copy with CopywriterAgent across all target languages.</p>
        </div>
      </div>

      <!-- Content Generator Controls -->
      <div class="bg-white border border-slate-200 rounded-2xl p-6 shadow-sm">
        <form id="generateForm" onsubmit="handleGenerateContent(event)" class="space-y-4">
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <label class="block text-xs font-semibold text-slate-700 mb-1">Select Campaign</label>
              <select id="genCampaignSelect" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500">
                <!-- Populated dynamically -->
              </select>
            </div>

            <div class="sm:col-span-2">
              <label class="block text-xs font-semibold text-slate-700 mb-1">Topic / Promotion Focus</label>
              <input type="text" id="genTopic" value="Exclusive summer gift box release with early-bird discount" class="w-full text-sm border border-slate-300 rounded-lg px-3 py-2 outline-none focus:ring-2 focus:ring-blue-500" placeholder="e.g. Summer special discount, product reveal" />
            </div>
          </div>

          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pt-2">
            <div class="flex items-center space-x-3 text-xs text-slate-500">
              <i class="fa-solid fa-circle-info text-blue-500"></i>
              <span>Generates multilingual drafts adapted to character limits and platform hashtags.</span>
            </div>
            <button type="submit" id="genSubmitBtn" class="inline-flex items-center space-x-2 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white font-semibold px-5 py-2.5 rounded-xl shadow transition">
              <i class="fa-solid fa-wand-sparkles"></i>
              <span>Generate Multi-Agent Drafts</span>
            </button>
          </div>
        </form>
      </div>

      <!-- Generated Drafts Grid -->
      <div class="flex justify-between items-center pt-2">
        <h2 class="text-lg font-bold text-slate-900">Generated Content Drafts</h2>
        <button onclick="publishAllDrafts()" class="text-xs bg-emerald-600 hover:bg-emerald-700 text-white font-semibold px-3 py-1.5 rounded-lg shadow-sm">
          <i class="fa-solid fa-paper-plane mr-1"></i> Publish All Drafts
        </button>
      </div>

      <div id="draftsGrid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <!-- Dynamically rendered -->
      </div>
    </section>

    <!-- ==================== TAB 4: PUBLISHING & TASKS ==================== -->
    <section id="section-schedules" class="hidden space-y-6">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 class="text-2xl font-bold text-slate-900">Publishing Queue & Tasks</h1>
          <p class="text-sm text-slate-500">PublisherAgent automated queue and status monitor.</p>
        </div>
        <div class="flex items-center space-x-2">
          <button onclick="loadTasks()" class="bg-white border border-slate-300 hover:bg-slate-50 text-slate-700 px-3 py-2 rounded-lg text-sm transition">
            <i class="fa-solid fa-arrow-rotate-right mr-1"></i> Refresh Tasks
          </button>
        </div>
      </div>

      <!-- Task Table -->
      <div class="bg-white border border-slate-200 rounded-2xl shadow-sm overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-left text-sm text-slate-600">
            <thead class="bg-slate-50 text-xs font-semibold uppercase text-slate-500 border-b border-slate-200">
              <tr>
                <th class="px-5 py-3.5">Platform</th>
                <th class="px-5 py-3.5">Task ID</th>
                <th class="px-5 py-3.5">Status</th>
                <th class="px-5 py-3.5">Published / Scheduled Time</th>
                <th class="px-5 py-3.5">External ID</th>
                <th class="px-5 py-3.5 text-right">Action</th>
              </tr>
            </thead>
            <tbody id="tasksTableBody" class="divide-y divide-slate-100">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>
    </section>
  </main>

  <footer class="bg-white border-t border-slate-200 py-4 mt-8">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 text-center text-xs text-slate-400">
      Multi-Agent Cross-Border & Domestic Social Media Marketing System • FastAPI + Pydantic + APScheduler
    </div>
  </footer>

  <script>
    let channelChartInstance = null;
    let engagementChartInstance = null;
    let currentCampaigns = [];
    let currentDrafts = [];
    let currentTasks = [];

    // Switch Navigation Tabs
    function switchTab(tabId) {
      const tabs = ['analytics', 'campaigns', 'studio', 'schedules'];
      tabs.forEach(t => {
        const btn = document.getElementById(`tab-${t}`);
        const sec = document.getElementById(`section-${t}`);
        if (t === tabId) {
          btn.classList.add('tab-active');
          sec.classList.remove('hidden');
        } else {
          btn.classList.remove('tab-active');
          sec.classList.add('hidden');
        }
      });
      if (tabId === 'analytics') loadAnalytics();
      if (tabId === 'campaigns') loadCampaigns();
      if (tabId === 'studio') { loadCampaigns(); loadDrafts(); }
      if (tabId === 'schedules') loadTasks();
    }

    // Notification toast
    function showToast(message, isError = false) {
      const toast = document.getElementById('toast');
      const toastMsg = document.getElementById('toastMsg');
      const toastIcon = document.getElementById('toastIcon');
      toastMsg.innerText = message;
      if (isError) {
        toastIcon.className = "fa-solid fa-circle-xmark text-rose-400 text-base";
      } else {
        toastIcon.className = "fa-solid fa-circle-check text-emerald-400 text-base";
      }
      toast.classList.remove('translate-y-[-20px]', 'opacity-0', 'pointer-events-none');
      setTimeout(() => {
        toast.classList.add('translate-y-[-20px]', 'opacity-0', 'pointer-events-none');
      }, 3500);
    }

    // Toggle Campaign Form
    function toggleCampaignForm() {
      const container = document.getElementById('campaignFormContainer');
      container.classList.toggle('hidden');
    }

    // Prefill Presets
    function prefillPreset(type) {
      if (type === 'fashion') {
        document.getElementById('campName').value = "Cross-Border Fashion & Gifts";
        document.getElementById('campObjective').value = "Drive summer gift box sales across China and international markets";
        document.getElementById('campBudget').value = 1500;
        document.getElementById('campVoice').value = "elegant, premium, warm, trendy";
        document.getElementById('campProduct').value = "Artisanal accessories and luxury gift sets crafted with heritage modern styling.";
      } else if (type === 'saas') {
        document.getElementById('campName').value = "Global AI SaaS Launch";
        document.getElementById('campObjective').value = "Acquire early signups and viral demo shares";
        document.getElementById('campBudget').value = 2000;
        document.getElementById('campVoice').value = "cutting-edge, concise, impactful";
        document.getElementById('campProduct').value = "Automated social media workflow platform powered by multi-agent intelligence.";
      }
    }

    // Platform badges styling helper
    function getPlatformMeta(platform) {
      const p = platform.toLowerCase();
      const meta = {
        meta: { name: 'Meta / FB', bg: 'bg-blue-100 text-blue-800 border-blue-200', icon: 'fa-brands fa-facebook' },
        instagram: { name: 'Instagram', bg: 'bg-pink-100 text-pink-800 border-pink-200', icon: 'fa-brands fa-instagram' },
        tiktok: { name: 'TikTok', bg: 'bg-slate-900 text-white border-slate-700', icon: 'fa-brands fa-tiktok' },
        x: { name: 'X / Twitter', bg: 'bg-slate-800 text-white border-slate-600', icon: 'fa-brands fa-x-twitter' },
        xiaohongshu: { name: 'Xiaohongshu', bg: 'bg-red-100 text-red-700 border-red-200', icon: 'fa-solid fa-book-bookmark' },
        douyin: { name: 'Douyin', bg: 'bg-neutral-800 text-neutral-100 border-neutral-700', icon: 'fa-solid fa-play' },
        wechat: { name: 'WeChat', bg: 'bg-emerald-100 text-emerald-800 border-emerald-200', icon: 'fa-brands fa-weixin' },
      };
      return meta[p] || { name: platform, bg: 'bg-slate-100 text-slate-800 border-slate-200', icon: 'fa-solid fa-share-nodes' };
    }

    // ==================== LOAD ANALYTICS ====================
    async function loadAnalytics() {
      try {
        const period = document.getElementById('periodSelect').value || 30;
        const res = await fetch(`/analytics/report?period_days=${period}`);
        const data = await res.json();

        // Update Totals
        const t = data.totals || {};
        document.getElementById('metric-impressions').innerText = (t.impressions || 415000).toLocaleString();
        document.getElementById('metric-clicks').innerText = (t.clicks || 18770).toLocaleString();
        document.getElementById('metric-ctr').innerText = `${t.ctr_percentage || 4.52}% CTR`;
        document.getElementById('metric-spend').innerText = `$${(t.spend || 910).toLocaleString()}`;
        document.getElementById('metric-revenue').innerText = `$${(t.estimated_revenue || 3094).toLocaleString()}`;
        document.getElementById('metric-roi').innerText = `+${t.roi_percentage || 240.0}%`;

        // Render Recommendations
        if (data.recommendations && data.recommendations.length > 0) {
          document.getElementById('recommendationsList').innerHTML = data.recommendations.map(r => `
            <li class="flex items-start space-x-2">
              <span class="text-blue-600 font-bold">•</span>
              <span>${r}</span>
            </li>
          `).join('');
        }

        // Render Anomalies
        if (data.anomalies && data.anomalies.length > 0) {
          document.getElementById('anomaliesList').innerHTML = data.anomalies.map(a => `
            <li class="flex items-start space-x-2">
              <span class="text-emerald-600 font-bold">•</span>
              <span>${a}</span>
            </li>
          `).join('');
        }

        // Render Charts
        renderCharts(data.channels || []);
      } catch (err) {
        console.error('Failed to load analytics:', err);
      }
    }

    function renderCharts(channels) {
      if (!channels || channels.length === 0) return;
      const labels = channels.map(c => c.platform);
      const impressions = channels.map(c => c.impressions);
      const views = channels.map(c => c.views);
      const clicks = channels.map(c => c.clicks);
      const engagements = channels.map(c => c.engagements);

      // Channel Chart (Bar)
      const ctxBar = document.getElementById('channelChart').getContext('2d');
      if (channelChartInstance) channelChartInstance.destroy();
      channelChartInstance = new Chart(ctxBar, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Impressions',
              data: impressions,
              backgroundColor: '#3b82f6',
              borderRadius: 6,
            },
            {
              label: 'Views',
              data: views,
              backgroundColor: '#6366f1',
              borderRadius: 6,
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'top' } },
          scales: { y: { beginAtZero: true, grid: { color: '#f1f5f9' } }, x: { grid: { display: false } } }
        }
      });

      // Engagement Chart (Doughnut)
      const ctxDonut = document.getElementById('engagementChart').getContext('2d');
      if (engagementChartInstance) engagementChartInstance.destroy();
      engagementChartInstance = new Chart(ctxDonut, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: engagements,
            backgroundColor: [
              '#3b82f6', '#ec4899', '#0f172a', '#ef4444', '#10b981', '#14b8a6', '#64748b'
            ],
            borderWidth: 2,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 10 } } } }
        }
      });
    }

    // ==================== CAMPAIGN ACTIONS ====================
    async function loadCampaigns() {
      try {
        const res = await fetch('/campaigns');
        currentCampaigns = await res.json();

        // Populate Select in Studio
        const select = document.getElementById('genCampaignSelect');
        select.innerHTML = currentCampaigns.map(c => `<option value="${c.id}">${c.name}</option>`).join('');

        // Populate Campaign List
        const list = document.getElementById('campaignList');
        if (currentCampaigns.length === 0) {
          list.innerHTML = `
            <div class="col-span-full bg-white p-8 rounded-2xl border border-dashed border-slate-300 text-center">
              <i class="fa-solid fa-bullhorn text-3xl text-slate-300 mb-2"></i>
              <p class="text-sm font-medium text-slate-600">No campaigns created yet.</p>
              <button onclick="seedDemoData()" class="mt-3 text-xs bg-blue-600 text-white px-3 py-1.5 rounded-lg">Seed Demo Campaign</button>
            </div>
          `;
          return;
        }

        list.innerHTML = currentCampaigns.map(c => {
          const budgetAlloc = c.strategy?.budget_allocation || {};
          const pillars = c.strategy?.content_pillars || [];
          return `
            <div class="bg-white rounded-2xl border border-slate-200 p-5 shadow-sm hover:shadow-md transition flex flex-col justify-between">
              <div>
                <div class="flex justify-between items-start mb-2">
                  <h3 class="font-bold text-slate-900 text-base line-clamp-1">${c.name}</h3>
                  <span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200 uppercase">${c.status}</span>
                </div>
                <p class="text-xs text-slate-500 mb-4 line-clamp-2">${c.objective}</p>

                <div class="space-y-2 mb-4 text-xs">
                  <div class="flex justify-between text-slate-600">
                    <span class="font-medium">Total Budget:</span>
                    <span class="font-bold text-slate-800">$${c.budget} ${c.currency}</span>
                  </div>
                  <div class="flex justify-between text-slate-600">
                    <span class="font-medium">Languages:</span>
                    <span>${c.languages.join(', ').toUpperCase()}</span>
                  </div>
                </div>

                <div class="mb-4">
                  <span class="block text-xs font-semibold text-slate-700 mb-1.5">Target Channels:</span>
                  <div class="flex flex-wrap gap-1.5">
                    ${(c.platforms || []).map(p => {
                      const m = getPlatformMeta(p);
                      return `<span class="inline-flex items-center space-x-1 text-xs px-2 py-0.5 rounded-md border ${m.bg}"><i class="${m.icon}"></i><span>${m.name}</span></span>`;
                    }).join('')}
                  </div>
                </div>

                ${pillars.length > 0 ? `
                  <div class="mb-4">
                    <span class="block text-xs font-semibold text-slate-700 mb-1">Content Pillars:</span>
                    <div class="flex flex-wrap gap-1">
                      ${pillars.map(p => `<span class="px-1.5 py-0.5 bg-slate-100 text-slate-600 rounded text-[11px]">${p}</span>`).join('')}
                    </div>
                  </div>
                ` : ''}
              </div>

              <div class="pt-3 border-t border-slate-100 flex justify-between items-center">
                <span class="text-[11px] text-slate-400 font-mono">${c.id.slice(0, 8)}...</span>
                <button onclick="triggerStudioFor('${c.id}')" class="inline-flex items-center space-x-1 text-xs font-semibold text-blue-600 hover:text-blue-800">
                  <span>Generate Content</span>
                  <i class="fa-solid fa-arrow-right text-[10px]"></i>
                </button>
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        console.error('Failed to load campaigns:', err);
      }
    }

    function triggerStudioFor(campaignId) {
      switchTab('studio');
      document.getElementById('genCampaignSelect').value = campaignId;
    }

    async function handleCreateCampaign(e) {
      e.preventDefault();
      const name = document.getElementById('campName').value;
      const objective = document.getElementById('campObjective').value;
      const budget = parseFloat(document.getElementById('campBudget').value) || 1000;
      const brand_voice = document.getElementById('campVoice').value;
      const frequency_per_day = parseInt(document.getElementById('campFrequency').value) || 2;
      const product_description = document.getElementById('campProduct').value;

      const checkedPlatforms = Array.from(document.querySelectorAll('input[name="platformCheck"]:checked')).map(cb => cb.value);
      const platforms = checkedPlatforms.length > 0 ? checkedPlatforms : ['meta', 'instagram', 'tiktok', 'xiaohongshu'];

      const payload = {
        name,
        objective,
        budget,
        currency: 'USD',
        platforms,
        brand_voice,
        product_description,
        schedule: {
          frequency_per_day,
          posting_times: ["11:30", "19:30"]
        },
        languages: ["en", "zh"]
      };

      try {
        const res = await fetch('/campaign/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        showToast("Campaign created and strategy generated!");
        toggleCampaignForm();
        loadCampaigns();
      } catch (err) {
        showToast("Error creating campaign", true);
      }
    }

    // ==================== CONTENT STUDIO ====================
    async function loadDrafts() {
      try {
        const res = await fetch('/drafts');
        currentDrafts = await res.json();
        const grid = document.getElementById('draftsGrid');

        if (!currentDrafts || currentDrafts.length === 0) {
          grid.innerHTML = `
            <div class="col-span-full bg-white p-8 rounded-2xl border border-dashed border-slate-300 text-center">
              <i class="fa-solid fa-pen-nib text-3xl text-slate-300 mb-2"></i>
              <p class="text-sm font-medium text-slate-600">No content drafts generated yet.</p>
              <p class="text-xs text-slate-400 mt-1">Select a campaign above and click Generate to start.</p>
            </div>
          `;
          return;
        }

        grid.innerHTML = currentDrafts.map(d => {
          const m = getPlatformMeta(d.platform);
          return `
            <div class="bg-white rounded-2xl border border-slate-200 p-5 shadow-sm hover:shadow-md transition flex flex-col justify-between">
              <div>
                <div class="flex justify-between items-center mb-3">
                  <span class="inline-flex items-center space-x-1.5 text-xs font-semibold px-2.5 py-1 rounded-lg border ${m.bg}">
                    <i class="${m.icon}"></i>
                    <span>${m.name}</span>
                  </span>
                  <span class="text-xs font-mono font-medium px-2 py-0.5 bg-slate-100 text-slate-600 rounded">
                    ${d.language ? d.language.toUpperCase() : 'EN'}
                  </span>
                </div>

                <h4 class="font-bold text-slate-900 text-sm mb-2 line-clamp-1">${d.title || 'Platform Draft'}</h4>
                <div class="p-3 bg-slate-50 rounded-xl text-xs text-slate-700 leading-relaxed mb-3 whitespace-pre-wrap font-sans">
                  ${d.body}
                </div>

                <div class="flex flex-wrap gap-1 mb-3">
                  ${(d.hashtags || []).map(h => `<span class="text-[11px] text-blue-600 font-medium">${h}</span>`).join(' ')}
                </div>

                ${d.call_to_action ? `
                  <div class="text-xs text-slate-500 italic mb-2">
                    <span class="font-semibold text-slate-700">CTA:</span> ${d.call_to_action}
                  </div>
                ` : ''}
              </div>

              <div class="pt-3 border-t border-slate-100 flex items-center justify-between">
                <span class="text-[11px] text-slate-400">${d.body.length} chars</span>
                <div class="flex space-x-2">
                  <button onclick="publishDraft('${d.id}', false)" class="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-2.5 py-1 rounded-lg transition">
                    <i class="fa-solid fa-clock mr-1"></i> Schedule
                  </button>
                  <button onclick="publishDraft('${d.id}', true)" class="text-xs bg-blue-600 hover:bg-blue-700 text-white font-semibold px-3 py-1 rounded-lg shadow-sm transition">
                    <i class="fa-solid fa-paper-plane mr-1"></i> Publish
                  </button>
                </div>
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        console.error('Failed to load drafts:', err);
      }
    }

    async function handleGenerateContent(e) {
      e.preventDefault();
      const campaign_id = document.getElementById('genCampaignSelect').value;
      const topic = document.getElementById('genTopic').value;

      try {
        const res = await fetch('/content/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            campaign_id,
            topic,
            count_per_platform: 1
          })
        });
        const drafts = await res.json();
        showToast(`Generated ${drafts.length} drafts across target platforms!`);
        loadDrafts();
      } catch (err) {
        showToast("Error generating drafts", true);
      }
    }

    async function publishDraft(draftId, publishNow = true) {
      try {
        const res = await fetch('/publish/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            content_draft_ids: [draftId],
            publish_now: publishNow
          })
        });
        const tasks = await res.json();
        showToast(publishNow ? "Post published smoothly (mock fallback)!" : "Post scheduled in queue!");
        loadTasks();
        loadAnalytics();
      } catch (err) {
        showToast("Publish error", true);
      }
    }

    async function publishAllDrafts() {
      if (!currentDrafts || currentDrafts.length === 0) return;
      const ids = currentDrafts.map(d => d.id);
      try {
        const res = await fetch('/publish/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            content_draft_ids: ids,
            publish_now: true
          })
        });
        const tasks = await res.json();
        showToast(`Published ${tasks.length} posts successfully!`);
        loadTasks();
        loadAnalytics();
      } catch (err) {
        showToast("Bulk publish error", true);
      }
    }

    // ==================== SCHEDULES & TASKS ====================
    async function loadTasks() {
      try {
        const res = await fetch('/publish/tasks');
        currentTasks = await res.json();
        const tbody = document.getElementById('tasksTableBody');

        if (!currentTasks || currentTasks.length === 0) {
          tbody.innerHTML = `
            <tr>
              <td colspan="6" class="px-5 py-8 text-center text-slate-400">
                No publish tasks recorded yet. Generate and publish drafts from Content Studio.
              </td>
            </tr>
          `;
          return;
        }

        tbody.innerHTML = currentTasks.map(t => {
          const m = getPlatformMeta(t.platform);
          let statusColor = "bg-slate-100 text-slate-700";
          if (t.status === 'published') statusColor = "bg-emerald-50 text-emerald-700 border border-emerald-200";
          if (t.status === 'scheduled') statusColor = "bg-blue-50 text-blue-700 border border-blue-200";
          if (t.status === 'publishing') statusColor = "bg-amber-50 text-amber-700 border border-amber-200 animate-pulse";
          if (t.status === 'failed') statusColor = "bg-rose-50 text-rose-700 border border-rose-200";

          const timeVal = t.published_at || t.scheduled_at || 'Instant';
          const formattedTime = timeVal !== 'Instant' ? new Date(timeVal).toLocaleString() : 'Executed';

          return `
            <tr class="hover:bg-slate-50 transition">
              <td class="px-5 py-4 whitespace-nowrap">
                <span class="inline-flex items-center space-x-1.5 text-xs font-semibold px-2 py-0.5 rounded-md border ${m.bg}">
                  <i class="${m.icon}"></i>
                  <span>${m.name}</span>
                </span>
              </td>
              <td class="px-5 py-4 whitespace-nowrap font-mono text-xs text-slate-500">${t.id.slice(0, 8)}...</td>
              <td class="px-5 py-4 whitespace-nowrap">
                <span class="px-2.5 py-1 text-xs font-semibold rounded-full uppercase ${statusColor}">${t.status}</span>
              </td>
              <td class="px-5 py-4 whitespace-nowrap text-xs text-slate-600">${formattedTime}</td>
              <td class="px-5 py-4 whitespace-nowrap font-mono text-xs text-slate-500">
                <span class="bg-slate-100 px-2 py-0.5 rounded border border-slate-200">${t.external_post_id || 'simulated'}</span>
              </td>
              <td class="px-5 py-4 whitespace-nowrap text-right text-xs">
                ${t.status === 'scheduled' ? `
                  <button onclick="publishDraft('${t.content_draft_id}', true)" class="text-blue-600 hover:text-blue-800 font-medium">Publish Now</button>
                ` : `
                  <span class="text-emerald-600 font-semibold"><i class="fa-solid fa-check mr-1"></i>Done</span>
                `}
              </td>
            </tr>
          `;
        }).join('');
      } catch (err) {
        console.error('Failed to load tasks:', err);
      }
    }

    // ==================== INSTANT DEMO SEED ====================
    async function seedDemoData() {
      const btn = document.getElementById('seedBtn');
      btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i><span>Seeding...</span>`;
      try {
        const res = await fetch('/demo/seed', { method: 'POST' });
        const result = await res.json();
        showToast("Sample campaign, drafts, and schedule seeded!");
        loadAnalytics();
        loadCampaigns();
        loadDrafts();
        loadTasks();
      } catch (err) {
        showToast("Error seeding demo data", true);
      } finally {
        btn.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i><span>Instant Demo Seed</span>`;
      }
    }

    // Initialize on page load
    document.addEventListener('DOMContentLoaded', () => {
      loadAnalytics();
      loadCampaigns();
      loadDrafts();
      loadTasks();
    });
  </script>
</body>
</html>
"""
