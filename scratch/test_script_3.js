
    let currentCampaignId = null;
    let currentPlatformFilter = 'all';
    let cachedDrafts = {};
    let cachedCampaigns = {};
    let cachedPlatforms = {};
    let currentUser = null;
    let attachedImage = null;

    // Toast helper
    function showToast(msg, isSuccess = true) {
      const toast = document.getElementById('toast');
      const toastMsg = document.getElementById('toastMsg');
      const toastIcon = document.getElementById('toastIcon');
      toastMsg.textContent = msg;
      toastIcon.textContent = isSuccess ? 'check_circle' : 'info';
      toastIcon.className = isSuccess ? 'material-symbols-outlined text-sm text-emerald-400' : 'material-symbols-outlined text-sm text-indigo-400';
      toast.classList.remove('opacity-0', 'pointer-events-none', 'translate-y-2');
      setTimeout(() => {
        toast.classList.add('opacity-0', 'pointer-events-none', 'translate-y-2');
      }, 3500);
    }

    // Modal toggles
    function openNewCampaignModal() { document.getElementById('newCampaignModal').classList.remove('hidden'); }
    function closeNewCampaignModal() { document.getElementById('newCampaignModal').classList.add('hidden'); }
    function openMcpModal() { loadMcpSettings(); document.getElementById('mcpApiModal').classList.remove('hidden'); }
    function closeMcpModal() { document.getElementById('mcpApiModal').classList.add('hidden'); }
    function openSchedulesModal() {
      loadSchedulesList();
      document.getElementById('schedulesModal').classList.remove('hidden');
    }
    function closeSchedulesModal() { document.getElementById('schedulesModal').classList.add('hidden'); }

    function toggleSidebar() {
      const sb = document.getElementById('sidebar');
      sb.classList.toggle('-ml-[280px]');
    }

    // Platform chip filter
    function togglePlatformChip(btn, platform) {
      document.querySelectorAll('.chip-btn').forEach(b => {
        b.classList.remove('active-chip', 'bg-indigo-500/20', 'text-indigo-300', 'border-indigo-500/30');
        b.classList.add('bg-white/[0.06]', 'text-zinc-300', 'border-white/5');
      });
      btn.classList.add('active-chip', 'bg-indigo-500/20', 'text-indigo-300', 'border-indigo-500/30');
      btn.classList.remove('bg-white/[0.06]', 'text-zinc-300', 'border-white/5');
      currentPlatformFilter = platform;
    }

    // =========================================================================
    // DYNAMIC USER IMAGE ATTACHMENT & PREVIEW PIPELINE
    // =========================================================================
    let attachedImageBase64 = null;
    let attachedImageName = '';

    function handleMediaSelect(e) {
      const file = e.target.files && e.target.files[0];
      if (file) {
        attachedImageName = file.name;
        const reader = new FileReader();
        reader.onload = function(evt) {
          attachedImageBase64 = evt.target.result;
          showAttachedImagePreview(attachedImageBase64, file.name);
          showToast('Image attached: ' + file.name, true);
        };
        reader.readAsDataURL(file);
      }
    }

    function showAttachedImagePreview(base64, name) {
      const previewBox = document.getElementById('attachedImagePreview');
      const thumb = document.getElementById('attachedThumb');
      const fname = document.getElementById('attachedFileName');
      if (previewBox && thumb) {
        thumb.src = base64;
        if (fname) fname.textContent = name || 'Uploaded Image';
        previewBox.classList.remove('hidden');
      }
      const label = document.getElementById('attachLabel');
      if (label) label.textContent = 'Image Attached ✓';
    }

    function clearAttachedImage() {
      attachedImageBase64 = null;
      attachedImageName = '';
      const input = document.getElementById('mediaFileInput');
      if (input) input.value = '';
      const previewBox = document.getElementById('attachedImagePreview');
      if (previewBox) previewBox.classList.add('hidden');
      const label = document.getElementById('attachLabel');
      if (label) label.textContent = 'Attach Media (+)';
    }

    function appendUserMessage(text, imgSrc) {
      const msgArea = document.getElementById('messagesContainer');
      const userBubble = document.createElement('div');
      userBubble.className = 'flex justify-end animate-fade-in';
      const userName = (currentUser && currentUser.full_name) ? currentUser.full_name : 'You';
      userBubble.innerHTML = `
        <div class="max-w-2xl glass-panel bg-white/[0.04] border border-white/[0.12] rounded-3xl rounded-tr-sm p-4 shadow-xl text-zinc-100 space-y-2">
          <div class="flex items-center justify-between text-[11px] text-zinc-400 pb-1">
            <span class="font-medium text-indigo-300 flex items-center gap-1.5">
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> ${escapeHtml(userName)}
            </span>
            <span>Just now</span>
          </div>
          ${text ? `<p class="text-sm leading-relaxed text-zinc-100">${escapeHtml(text)}</p>` : ''}
          ${imgSrc ? `
            <div class="rounded-2xl overflow-hidden mt-2 max-h-64 border border-white/10 shadow-lg bg-black/40">
              <img src="${imgSrc}" class="w-full h-full object-contain max-h-64"/>
            </div>
          ` : ''}
        </div>
      `;
      msgArea.appendChild(userBubble);
      scrollToBottom();
    }

    // =========================================================================
    // CHAT ENGINE HELPERS & EVENT HANDLERS
    // =========================================================================
    function handleInputKey(event) {
      if (!event && typeof window !== 'undefined') {
        event = window.event;
      }
      if (!event) return;
      if (event.key === 'Enter' && !event.shiftKey) {
        if (event.preventDefault) event.preventDefault();
        submitChat();
      }
    }

    function appendThinkingIndicator(customId) {
      const tid = customId || ('thinking-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7));
      const hero = document.getElementById('emptyHeroState');
      if (hero) hero.classList.add('hidden');
      const msgArea = document.getElementById('messagesContainer');
      if (!msgArea) return tid;
      msgArea.classList.remove('hidden');

      const existing = document.getElementById(tid);
      if (existing) return tid;

      const thinkingBubble = document.createElement('div');
      thinkingBubble.id = tid;
      thinkingBubble.className = 'flex items-start gap-3 animate-fade-in thinking-indicator';
      thinkingBubble.innerHTML = `
        <div class="w-7 h-7 rounded-xl bg-gradient-to-tr from-indigo-500 to-emerald-400 flex items-center justify-center text-white shrink-0 shadow-md">
          <span class="material-symbols-outlined text-sm animate-spin">progress_activity</span>
        </div>
        <div class="glass-panel p-3.5 rounded-2xl rounded-tl-sm border border-white/10 bg-white/[0.03] text-zinc-300 text-xs space-y-1.5 max-w-sm shadow-lg">
          <div class="flex items-center gap-2">
            <span class="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-ping"></span>
            <span class="font-semibold text-white text-xs">OmniFlow AI is thinking...</span>
          </div>
          <p class="text-[11px] text-zinc-400">Synthesizing cross-border marketing strategy & multimodal creative...</p>
        </div>
      `;
      msgArea.appendChild(thinkingBubble);
      scrollToBottom();
      return tid;
    }

    function removeThinkingIndicator(tid) {
      if (tid) {
        const el = document.getElementById(tid);
        if (el) {
          el.remove();
          return;
        }
      }
      document.querySelectorAll('.thinking-indicator').forEach(el => el.remove());
    }

    function appendAiErrorMessage(text) {
      const hero = document.getElementById('emptyHeroState');
      if (hero) hero.classList.add('hidden');
      const msgArea = document.getElementById('messagesContainer');
      if (!msgArea) return;
      msgArea.classList.remove('hidden');

      const errCard = document.createElement('div');
      errCard.className = 'space-y-2 animate-fade-in';
      errCard.innerHTML = `
        <div class="flex items-center gap-2 text-xs text-rose-400">
          <div class="w-6 h-6 rounded-lg bg-rose-500/20 border border-rose-500/30 flex items-center justify-center text-rose-400">
            <span class="material-symbols-outlined text-sm">error</span>
          </div>
          <span class="text-white font-medium">Generation Error</span>
          <span>•</span>
          <span class="text-rose-400 font-mono text-[11px]">Pipeline Notice</span>
        </div>
        <div class="glass-panel p-4 rounded-2xl border border-rose-500/25 bg-rose-950/20 text-rose-200 text-xs leading-relaxed max-w-xl">
          <p class="font-medium">${escapeHtml(text)}</p>
          <p class="text-[11px] text-zinc-400 mt-1">Please verify your server connection or try a different prompt.</p>
        </div>
      `;
      msgArea.appendChild(errCard);
      scrollToBottom();
    }

    function resetToNewChat() {
      removeThinkingIndicator();
      const msgArea = document.getElementById('messagesContainer');
      if (msgArea) {
        msgArea.innerHTML = '';
        msgArea.classList.add('hidden');
      }
      const hero = document.getElementById('emptyHeroState');
      if (hero) {
        hero.classList.remove('hidden');
      }
      const input = document.getElementById('chatInput');
      if (input) {
        input.value = '';
        try { input.focus(); } catch (_) {}
      }
      clearAttachedImage();
      cachedDrafts = {};
      showToast('Started new conversation', true);
    }

    function fillAndSend(prompt) {
      const input = document.getElementById('chatInput');
      if (input) {
        input.value = prompt;
      }
      submitChat();
    }

    function simulateVoice() {
      showToast('Voice input listening... (Microphone active)');
      const input = document.getElementById('chatInput');
      if (input) {
        input.placeholder = 'Listening to voice directive...';
        setTimeout(() => {
          input.value = 'Create a viral multi-platform campaign for our new ambient lighting drop';
          input.placeholder = 'Ask OmniFlow to create campaigns, generate copy, or analyze ROI...';
          showToast('Voice directive transcribed!', true);
        }, 1200);
      }
    }

    // =========================================================================
    // DYNAMIC CHAT SUBMISSION & VISION PIPELINE
    // =========================================================================
    async function submitChat() {
      const input = document.getElementById('chatInput');
      if (!input) return;
      const text = input.value.trim();
      const currentImage = attachedImageBase64;
      if (!text && !currentImage) return;

      // Hide empty hero state, reveal messages container
      const hero = document.getElementById('emptyHeroState');
      if (hero) hero.classList.add('hidden');
      const msgArea = document.getElementById('messagesContainer');
      if (msgArea) msgArea.classList.remove('hidden');

      // 1. Append User Message with REAL image thumbnail
      appendUserMessage(text, currentImage);
      input.value = '';
      clearAttachedImage();

      // 2. Show AI Thinking Indicator (returns element ID)
      const thinkingId = appendThinkingIndicator();
      scrollToBottom();

      const lower = text.toLowerCase();

      // Check for standalone helper commands if no image attached
      if (!currentImage) {
        if (lower.startsWith('/boost') || lower === 'boost') {
          removeThinkingIndicator(thinkingId);
          await handleBoostIntent(text);
          return;
        } else if (lower === 'analyze' || lower === 'report' || lower === 'roas') {
          removeThinkingIndicator(thinkingId);
          await handleAnalyticsIntent();
          return;
        }
      }

      // 3. Invoke Dynamic Content Generation with Vision & gpt-5.6-luna
      try {
        const payload = {
          prompt: text,
          topic: text,
          image_base64: currentImage,
          campaign_id: currentCampaignId || 'demo-campaign'
        };
        if (currentPlatformFilter && currentPlatformFilter !== 'all') {
          payload.platforms = [currentPlatformFilter];
        }

        const res = await fetch('/content/generate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });

        if (res.ok) {
          const data = await res.json();
          renderDynamicCreativeCard(text, data, currentImage);
        } else {
          let errDetail = 'Generation failed (HTTP ' + res.status + ')';
          try {
            const errJson = await res.json();
            if (errJson && errJson.detail) errDetail = errJson.detail;
          } catch (_) {}
          console.error('submitChat generation failure:', errDetail);
          appendAiErrorMessage(errDetail);
          showToast(errDetail, false);
        }
      } catch (err) {
        console.error('submitChat network error:', err);
        const errDetail = 'Network error in generation pipeline: ' + (err.message || 'Server unreachable');
        appendAiErrorMessage(errDetail);
        showToast(errDetail, false);
      } finally {
        removeThinkingIndicator(thinkingId);
        scrollToBottom();
      }
    }

    // =========================================================================
    // DYNAMIC PREVIEW CARD RENDERING (USER REAL IMAGE + META & TIKTOK TABS)
    // =========================================================================
    function renderDynamicCreativeCard(userPrompt, data, userImageBase64) {
      if (Array.isArray(data)) {
        data = {
          copy: data[0] ? data[0].body : '',
          hashtags: data[0] ? data[0].hashtags : [],
          drafts: data,
          draft_id: data[0] ? data[0].id : null,
          image_base64: data[0] ? data[0].image_base64 : null
        };
      }
      const cardId = 'card-' + Date.now();

      const copyText = data.copy || (data.drafts && data.drafts[0] ? data.drafts[0].body : (userPrompt || 'Modern Innovation'));
      let hashtagsArr = [];
      if (Array.isArray(data.hashtags)) {
        hashtagsArr = data.hashtags;
      } else if (typeof data.hashtags === 'string') {
        hashtagsArr = data.hashtags.split(/\s+/).filter(t => t.length > 0);
      } else if (data.drafts && data.drafts[0] && data.drafts[0].hashtags) {
        hashtagsArr = data.drafts[0].hashtags;
      }

      const activeImage = userImageBase64 || data.image_base64 || '/static/assets/images/smart_device.jpg';

      // Store in card state
      cachedDrafts[cardId] = {
        prompt: userPrompt,
        copy: copyText,
        hashtags: hashtagsArr,
        image_base64: userImageBase64 || data.image_base64 || null,
        active_image: activeImage,
        draft_id: data.draft_id || (data.drafts && data.drafts[0] ? data.drafts[0].id : 'draft-1'),
        currentPlatform: 'meta',
        tiktokCopy: (data.drafts && data.drafts[1]) ? data.drafts[1].body : `🔥 Check this out! ${copyText}`
      };
      window.currentActiveCaption = copyText;
      window.currentActiveImage = userImageBase64 || data.image_base64 || null;
      window.currentActiveCardId = cardId;


      const msgArea = document.getElementById('messagesContainer');
      const card = document.createElement('div');
      card.id = cardId;
      card.className = 'space-y-4 animate-fade-in';
      card.innerHTML = `
        <div class="flex items-center gap-2 text-xs text-zinc-400">
          <div class="w-6 h-6 rounded-lg bg-gradient-to-tr from-indigo-500 to-emerald-400 flex items-center justify-center text-white shadow-sm">
            <span class="material-symbols-outlined text-sm">auto_awesome</span>
          </div>
          <span class="text-white font-medium">OmniFlow 4.5 Vision</span>
          <span>•</span>
          <span class="text-emerald-400 font-mono text-[11px]">gpt-5.6-luna · Dynamic Creative Synthesized</span>
        </div>

        <div class="glass-panel rounded-3xl p-6 space-y-5 border border-white/[0.08] shadow-2xl relative overflow-hidden bg-[#0D1017]/90">
          <div>
            <h3 class="text-base font-bold text-white flex items-center gap-2">
              <span>Dynamic AI Creative Preview</span>
              <span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">User Asset Active 🟢</span>
            </h3>
            <p class="text-xs text-zinc-400 mt-1">Multi-modal tone synthesis adapted to your uploaded image and target audience.</p>
          </div>

          <!-- Segmented Platform Tabs (Meta & TikTok Priority) -->
          <div class="flex items-center gap-1.5 p-1 bg-black/40 backdrop-blur-md rounded-2xl border border-white/5 w-fit overflow-x-auto max-w-full">
            <button onclick="switchDynamicCardPlatform('${cardId}', 'meta')" id="${cardId}-tab-meta" class="card-tab px-4 py-1.5 rounded-xl bg-white/[0.14] text-white text-xs font-semibold shadow-sm flex items-center gap-2 transition">
              <img src="/static/assets/logos/meta.svg" class="w-4 h-4 rounded-md"/><span>Meta Feed</span>
            </button>
            <button onclick="switchDynamicCardPlatform('${cardId}', 'tiktok')" id="${cardId}-tab-tiktok" class="card-tab px-4 py-1.5 rounded-xl text-zinc-400 hover:text-white text-xs font-medium flex items-center gap-2 transition">
              <img src="/static/assets/logos/tiktok.svg" class="w-4 h-4 rounded-md"/><span>TikTok</span>
            </button>
          </div>

          <!-- Dynamic Mockup Display -->
          <div class="grid grid-cols-1 md:grid-cols-12 gap-5 bg-black/50 p-5 rounded-2xl border border-white/[0.08]">
            <!-- Creative Real Uploaded Thumbnail -->
            <div class="md:col-span-5 relative group overflow-hidden rounded-2xl bg-zinc-900 aspect-square border border-white/10 flex items-center justify-center">
              <img id="${cardId}-img" src="${activeImage}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"/>
              <div class="absolute top-3 left-3 px-2 py-1 rounded-lg bg-black/70 backdrop-blur-md border border-white/10 text-[10px] font-mono text-emerald-300 flex items-center gap-1.5">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
                <span>${userImageBase64 ? 'Live User Photo' : 'Asset Preview'}</span>
              </div>
            </div>

            <!-- Generated Copy & Hashtags -->
            <div class="md:col-span-7 flex flex-col justify-between space-y-4">
              <div class="space-y-3">
                <div class="flex items-center justify-between">
                  <span id="${cardId}-channelLabel" class="px-2.5 py-1 rounded-lg bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 text-[11px] font-semibold uppercase flex items-center gap-1.5">
                    <img src="/static/assets/logos/meta.svg" class="w-3.5 h-3.5"/> Meta Feed
                  </span>
                  <span class="text-[11px] text-zinc-400 font-mono">Vision Cohort #1</span>
                </div>
                <div id="${cardId}-body" class="text-xs text-zinc-200 leading-relaxed font-normal whitespace-pre-line max-h-56 overflow-y-auto pr-1 select-text">${escapeHtml(copyText)}</div>
                <div id="${cardId}-tags" class="flex flex-wrap gap-1.5 pt-1">
                  ${hashtagsArr.map(t => `<span class="px-2 py-0.5 rounded-full bg-white/[0.04] text-[11px] text-zinc-400 font-mono border border-white/5">${escapeHtml(t.startsWith('#') ? t : '#' + t)}</span>`).join('')}
                </div>
              </div>

              <div class="pt-3 border-t border-white/[0.08] flex items-center justify-between text-xs text-zinc-400">
                <span class="flex items-center gap-1 text-emerald-400 font-mono text-[11px]">
                  <span class="material-symbols-outlined text-xs">auto_graph</span> Projected Conversion: 4.85%
                </span>
                <span id="${cardId}-ctaTitle" class="text-white font-medium">${escapeHtml(data.call_to_action || (data.drafts && data.drafts[0] && data.drafts[0].call_to_action) || 'Learn More')}</span>
              </div>
            </div>
          </div>

          <!-- Instant Visual Success Banner Area -->
          <div id="${cardId}-bannerArea" class="hidden my-2"></div>

          <!-- Real Facebook Dispatch Action Bar -->
          <div class="pt-3 border-t border-white/[0.08] flex flex-wrap items-center justify-between gap-3">
            <div class="flex items-center gap-2.5 flex-wrap">
              <button onclick="publishCurrentCreative('${cardId}')" id="publishBtn" class="publish-fb-btn bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-2.5 px-6 rounded-xl shadow-lg cursor-pointer flex items-center gap-2 transition active:scale-95 text-xs">
                <span class="material-symbols-outlined text-base font-bold">check_circle</span>
                <span>Approve & Publish to Facebook</span>
              </button>
              <button onclick="regenerateCardDraft('${cardId}')" class="px-3.5 py-2 rounded-xl bg-white/[0.06] hover:bg-white/[0.1] border border-white/[0.1] text-zinc-200 text-xs font-medium flex items-center gap-1.5 transition">
                <span class="material-symbols-outlined text-sm">refresh</span>
                <span>Regenerate</span>
              </button>
            </div>
            <button onclick="copyDraftText('${cardId}')" class="px-3 py-2 rounded-xl bg-white/[0.04] hover:bg-white/[0.08] text-zinc-400 hover:text-white text-xs transition flex items-center gap-1.5">
              <span class="material-symbols-outlined text-sm">content_copy</span>
              <span>Copy Copy</span>
            </button>
          </div>

        </div>
      `;
      msgArea.appendChild(card);
      scrollToBottom();
    }

    function regenerateCardDraft(cardId) {
      const cardData = cachedDrafts[cardId];
      const p = (cardData && cardData.prompt) ? cardData.prompt : 'product';
      fillAndSend(`Regenerate copy with more urgency for ${p}`);
    }

    function switchDynamicCardPlatform(cardId, platform) {
      const card = document.getElementById(cardId);
      if (!card) return;

      const data = cachedDrafts[cardId] || {};
      data.currentPlatform = platform;

      card.querySelectorAll('.card-tab').forEach(t => {
        t.classList.remove('bg-white/[0.14]', 'text-white', 'font-semibold');
        t.classList.add('text-zinc-400');
      });
      const activeTab = document.getElementById(`${cardId}-tab-${platform}`);
      if (activeTab) {
        activeTab.classList.add('bg-white/[0.14]', 'text-white', 'font-semibold');
        activeTab.classList.remove('text-zinc-400');
      }

      const channelLabel = document.getElementById(`${cardId}-channelLabel`);
      const bodyEl = document.getElementById(`${cardId}-body`);

      if (platform === 'tiktok') {
        if (channelLabel) channelLabel.innerHTML = '<img src="/static/assets/logos/tiktok.svg" class="w-3.5 h-3.5"/> TikTok Viral';
        if (bodyEl) bodyEl.textContent = data.tiktokCopy || data.copy;
      } else {
        if (channelLabel) channelLabel.innerHTML = '<img src="/static/assets/logos/meta.svg" class="w-3.5 h-3.5"/> Meta Feed';
        if (bodyEl) bodyEl.textContent = data.copy;
      }
    }

    // =========================================================================
    // REAL FACEBOOK PHOTO / FEED PUBLISHING (POST /publish/schedule)
    // =========================================================================
    async function publishCurrentCreative(payload) {
      console.log(">>> [EXECUTING REAL FETCH TO BACKEND]", payload);

      // 1. Get the active data (from arguments or global active creative state)
      let captionText = (payload && typeof payload === 'object' && payload.caption) ? payload.caption : (window.currentActiveCaption || "");
      let imageB64 = (payload && typeof payload === 'object' && (payload.image_base64 || payload.image)) ? (payload.image_base64 || payload.image) : (window.currentActiveImage || "");

      let cardId = null;
      if (typeof payload === 'string') {
        cardId = payload;
        const cardData = (typeof cachedDrafts !== 'undefined' && cachedDrafts[cardId]) ? cachedDrafts[cardId] : {};
        const bodyEl = document.getElementById(`${cardId}-body`);
        if (!captionText) captionText = (bodyEl ? bodyEl.textContent : '') || cardData.copy || '';
        if (!imageB64) {
          imageB64 = cardData.image_base64 || window.currentActiveImage || "";
          if (!imageB64) {
            const imgEl = document.getElementById(`${cardId}-img`);
            if (imgEl && imgEl.src && imgEl.src.startsWith('data:image')) {
              imageB64 = imgEl.src;
            }
          }
        }
      }

      if (!captionText) {
        captionText = window.currentActiveCaption || document.getElementById('chatInput')?.value || "Exclusive OmniFlow Product Drop";
      }
      if (!imageB64) {
        imageB64 = window.currentActiveImage || (typeof attachedImageBase64 !== 'undefined' ? attachedImageBase64 : "");
      }

      // 2. Visual loading state
      const btn = document.querySelector("#publishBtn")
        || (cardId && document.getElementById(`${cardId}-publishFbBtn`))
        || (typeof event !== 'undefined' ? (event?.target?.closest?.('button') || event?.target) : null)
        || document.querySelector('.publish-fb-btn');

      if (btn) {
        btn.innerText = "Publishing to Meta...";
        btn.disabled = true;
      }

      try {
        const response = await fetch('/publish/schedule', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            platform: 'meta',
            caption: captionText,
            image_base64: imageB64,
            page_id: '101728504668130'
          })
        });

        const result = await response.json();
        console.log(">>> [BACKEND PUBLISH RESPONSE]", result);

        if (response.ok && (result.success || result.post_id || result.status === 'scheduled' || result.status === 'published')) {
          alert("Success! Published to Facebook (Mai boovoo).\nPost ID: " + (result.post_id || "OK"));
          if (btn) {
            btn.innerText = "Published to Facebook";
            btn.classList.remove('bg-emerald-600', 'hover:bg-emerald-700');
            btn.classList.add('bg-green-700');
          }
          const postUrl = result.post_url || (result.post_id ? `https://facebook.com/${result.post_id}` : 'https://facebook.com');
          const bannerArea = cardId ? document.getElementById(`${cardId}-bannerArea`) : document.querySelector('[id$="-bannerArea"]');
          if (bannerArea) {
            bannerArea.innerHTML = `
              <div class="p-3.5 rounded-xl bg-emerald-500/20 border border-emerald-500/40 text-emerald-300 text-xs flex flex-wrap items-center justify-between gap-2 shadow-lg animate-fade-in my-2">
                <span class="font-semibold flex items-center gap-1.5">
                  <span class="w-2 h-2 rounded-full bg-emerald-400 animate-ping"></span>
                  <span>🟢 Successfully published to Mai boovoo!</span>
                </span>
                <a href="${postUrl}" target="_blank" rel="noopener noreferrer" class="px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition flex items-center gap-1 text-xs shadow-md">
                  <span>View Post on Facebook</span>
                  <span class="material-symbols-outlined text-xs">open_in_new</span>
                </a>
              </div>
            `;
            bannerArea.classList.remove('hidden');
          }
        } else {
          alert("Publish Failed: " + (result.detail || result.error || JSON.stringify(result)));
          if (btn) {
            btn.innerText = "Try Again";
            btn.disabled = false;
          }
        }
      } catch (error) {
        console.error(">>> [PUBLISH NETWORK ERROR]", error);
        alert("Network error: " + error.message);
        if (btn) {
          btn.innerText = "Try Again";
          btn.disabled = false;
        }
      }
    }

    const approveAndPublish = publishCurrentCreative;
    window.publishCurrentCreative = publishCurrentCreative;
    window._publishCurrentCreative = publishCurrentCreative;
    window.approveAndPublish = publishCurrentCreative;


    function copyDraftText(cardId) {
      const body = document.getElementById(`${cardId}-body`).textContent;
      navigator.clipboard.writeText(body);
      showToast('Copy text copied to clipboard!');
    }

    // Append Campaign Card from AI creation
    function appendAiCampaignCard(camp) {
      if (camp && camp.id) cachedCampaigns[camp.id] = camp;
      const msgArea = document.getElementById('messagesContainer');
      const card = document.createElement('div');
      card.className = 'space-y-4 animate-fade-in';
      card.innerHTML = `
        <div class="flex items-center gap-2 text-xs text-zinc-400">
          <div class="w-6 h-6 rounded-lg bg-gradient-to-tr from-indigo-500 to-emerald-400 flex items-center justify-center text-white">
            <span class="material-symbols-outlined text-sm">rocket_launch</span>
          </div>
          <span class="text-white font-medium">Omni-Marketing 4.5</span>
          <span>•</span>
          <span class="text-emerald-400 font-mono text-[11px]">POST /campaign/create · Strategy Deployed</span>
        </div>

        <div class="glass-panel rounded-3xl p-6 space-y-4 border border-white/[0.08] shadow-2xl">
          <div class="flex items-center justify-between">
            <h3 class="text-base font-bold text-white">${escapeHtml(camp.name)}</h3>
            <span class="px-2.5 py-0.5 rounded-full text-xs bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Status: Planned</span>
          </div>
          <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <div class="p-3 rounded-xl bg-white/[0.02] border border-white/5">
              <span class="text-zinc-500 block text-[10px]">Total Budget</span>
              <span class="text-white font-bold text-sm">$${camp.budget} ${camp.currency || 'USD'}</span>
            </div>
            <div class="p-3 rounded-xl bg-white/[0.02] border border-white/5">
              <span class="text-zinc-500 block text-[10px]">Target Channels</span>
              <span class="text-indigo-400 font-bold text-sm">${(camp.platforms || []).length} Channels</span>
            </div>
            <div class="p-3 rounded-xl bg-white/[0.02] border border-white/5">
              <span class="text-zinc-500 block text-[10px]">Campaign ID</span>
              <span class="text-zinc-300 font-mono text-[11px] truncate block">${camp.id.substring(0, 8)}...</span>
            </div>
            <div class="p-3 rounded-xl bg-white/[0.02] border border-white/5">
              <span class="text-zinc-500 block text-[10px]">Strategy</span>
              <span class="text-emerald-400 font-medium">Bandit Dynamic</span>
            </div>
          </div>
          <div class="flex items-center justify-end gap-2 pt-2">
            <button onclick="synthesizeDraftsForCampaign('${camp.id}')" class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-semibold text-xs shadow-md transition">
              Synthesize Creative Copies →
            </button>
          </div>
        </div>
      `;
      msgArea.appendChild(card);
    }

    function synthesizeDraftsForCampaign(campId) {
      const camp = cachedCampaigns[campId];
      const name = (camp && camp.name) ? camp.name : 'current campaign';
      fillAndSend(`Generate marketing drafts for campaign ${name}`);
    }

    // =========================================================================
    // CAMPAIGN MANAGEMENT & DETAILED INSPECTION PIPELINE
    // =========================================================================
    async function loadCampaigns() {
      try {
        const res = await fetch('/campaigns');
        if (!res.ok) return;
        const campaigns = await res.json();
        if (!Array.isArray(campaigns)) return;

        campaigns.forEach(c => {
          if (c && c.id) cachedCampaigns[c.id] = c;
        });

        if (campaigns.length > 0 && !currentCampaignId) {
          currentCampaignId = campaigns[0].id;
        }

        const activeCamp = campaigns.find(c => c.id === currentCampaignId) || campaigns[0];
        const headerLabel = document.getElementById('headerCampaignLabel');
        if (headerLabel && activeCamp) {
          headerLabel.textContent = `${activeCamp.name} — AI Swarm Active`;
        }

        const sidebarList = document.getElementById('sidebarCampaignsList');
        if (sidebarList) {
          if (campaigns.length === 0) {
            sidebarList.innerHTML = '<div class="text-zinc-500 text-[11px] px-3 py-2">No active campaigns yet.</div>';
          } else {
            sidebarList.innerHTML = campaigns.map(c => `
              <div onclick="openCampaignDetailsModal('${c.id}')" class="flex items-center gap-2.5 px-3 py-2 rounded-xl text-xs font-medium text-white hover:bg-white/[0.09] bg-white/[0.05] border border-white/5 shadow-sm cursor-pointer transition active:scale-[0.98]">
                <span class="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.7)] ${c.status === 'active' ? 'animate-pulse' : ''}"></span>
                <span class="flex-1 truncate">${escapeHtml(c.name)}</span>
                <span class="text-[10px] font-mono text-zinc-400">${(c.platforms || []).length} ch</span>
              </div>
            `).join('');
          }
        }
      } catch (err) {
        console.warn('loadCampaigns error:', err);
      }
    }

    async function handleCampaignModalSubmit(event) {
      if (event && event.preventDefault) event.preventDefault();
      const nameEl = document.getElementById('modalCampName');
      const objEl = document.getElementById('modalCampObj');
      const budgetEl = document.getElementById('modalCampBudget');
      const descEl = document.getElementById('modalCampDesc');
      const submitBtn = document.getElementById('modalCampSubmitBtn');

      const name = nameEl ? nameEl.value.trim() : '';
      if (!name) return;

      const objective = objEl ? objEl.value : 'brand_awareness';
      const budget = budgetEl ? (parseFloat(budgetEl.value) || 1500) : 1500;
      const desc = descEl ? descEl.value.trim() : '';

      const checkedBoxes = document.querySelectorAll('input[name="mplatform"]:checked');
      const platforms = Array.from(checkedBoxes).map(cb => cb.value);

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Deploying...';
      }

      try {
        const res = await fetch('/campaign/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name,
            objective,
            budget,
            currency: 'USD',
            platforms: platforms.length ? platforms : ['meta', 'tiktok'],
            product_description: desc
          })
        });

        if (res.ok) {
          const camp = await res.json();
          currentCampaignId = camp.id;
          closeNewCampaignModal();
          showToast(`Campaign "${camp.name}" deployed!`, true);
          appendAiCampaignCard(camp);
          await loadCampaigns();
        } else {
          showToast('Failed to create campaign', false);
        }
      } catch (err) {
        showToast('Error creating campaign: ' + err.message, false);
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.innerHTML = '<span>Deploy Strategy</span><span class="material-symbols-outlined text-sm">arrow_forward</span>';
        }
      }
    }

    async function openCampaignDetailsModal(campaignId) {
      currentCampaignId = campaignId;
      const modal = document.getElementById('campaignDetailsModal');
      if (!modal) return;
      modal.classList.remove('hidden');

      try {
        const res = await fetch(`/campaign/${campaignId}/details`);
        if (res.ok) {
          const data = await res.json();
          renderCampaignDetailsModalContent(data);
          return;
        }
        const resCamp = await fetch(`/campaign/${campaignId}`);
        if (resCamp.ok) {
          const camp = await resCamp.json();
          renderCampaignDetailsModalContent({ campaign: camp, drafts: [], tasks: [] });
        }
      } catch (err) {
        console.warn('Error fetching campaign details:', err);
      }
    }

    function renderCampaignDetailsModalContent(data) {
      const camp = data.campaign || {};
      const drafts = data.drafts || [];
      const tasks = data.tasks || [];

      const cdTitle = document.getElementById('cdTitle');
      if (cdTitle) cdTitle.textContent = camp.name || 'Campaign Strategy';
      const cdObjective = document.getElementById('cdObjective');
      if (cdObjective) cdObjective.textContent = camp.objective || 'Cross-border autonomous strategy';
      const cdBudget = document.getElementById('cdBudget');
      if (cdBudget) cdBudget.textContent = '$' + (camp.budget || 0).toLocaleString() + ' ' + (camp.currency || 'USD');
      const cdPlatformsCount = document.getElementById('cdPlatformsCount');
      if (cdPlatformsCount) cdPlatformsCount.textContent = (camp.platforms || []).length + ' Active';
      const cdDraftsCount = document.getElementById('cdDraftsCount');
      if (cdDraftsCount) cdDraftsCount.textContent = drafts.length + ' Drafts';
      const cdTasksCount = document.getElementById('cdTasksCount');
      if (cdTasksCount) cdTasksCount.textContent = tasks.length + ' Tasks';
      const cdIdLabel = document.getElementById('cdIdLabel');
      if (cdIdLabel) cdIdLabel.textContent = 'ID: ' + (camp.id ? camp.id.substring(0, 8) : 'demo');
      const cdStatusBadge = document.getElementById('cdStatusBadge');
      if (cdStatusBadge) cdStatusBadge.textContent = (camp.status || 'ACTIVE').toUpperCase();
      const cdVoiceAndDesc = document.getElementById('cdVoiceAndDesc');
      if (cdVoiceAndDesc) cdVoiceAndDesc.textContent = (camp.brand_voice ? `Tone: ${camp.brand_voice}. ` : '') + (camp.product_description || '');

      const cdBudgetPills = document.getElementById('cdBudgetPills');
      if (cdBudgetPills) {
        const alloc = camp.channel_allocation || {};
        const platforms = camp.platforms || Object.keys(alloc);
        cdBudgetPills.innerHTML = platforms.map(p => {
          const share = alloc[p] != null ? Math.round(alloc[p] * 100) + '%' : 'Optimized';
          return `<span class="px-2.5 py-1 rounded-lg bg-white/[0.04] border border-white/10 text-[11px] text-zinc-300 font-mono">${escapeHtml(p)}: ${share}</span>`;
        }).join('');
      }

      const cdContentPillars = document.getElementById('cdContentPillars');
      if (cdContentPillars) {
        const pillars = camp.content_pillars && camp.content_pillars.length ? camp.content_pillars : ['Product Showcase', 'Lifestyle & Craft', 'Community & Perks'];
        cdContentPillars.innerHTML = pillars.map(pil => `<span class="px-2 py-0.5 rounded-full bg-indigo-500/10 text-indigo-300 border border-indigo-500/20 text-[10px] font-mono">${escapeHtml(pil)}</span>`).join('');
      }

      const cdDraftsList = document.getElementById('cdDraftsList');
      if (cdDraftsList) {
        if (drafts.length === 0) {
          cdDraftsList.innerHTML = '<div class="text-zinc-500 text-xs py-2">No copies synthesized yet. Click below to generate drafts.</div>';
        } else {
          cdDraftsList.innerHTML = drafts.map(d => `
            <div class="p-3 rounded-xl bg-white/[0.02] border border-white/5 space-y-1">
              <div class="flex items-center justify-between text-[10px]">
                <span class="px-2 py-0.5 rounded font-mono bg-indigo-500/15 text-indigo-300 uppercase">${escapeHtml(d.platform || 'meta')}</span>
                <span class="text-zinc-500">${escapeHtml(d.language || 'en')}</span>
              </div>
              <p class="text-[11px] text-zinc-300 line-clamp-2">${escapeHtml(d.body || '')}</p>
            </div>
          `).join('');
        }
      }

      const cdTasksList = document.getElementById('cdTasksList');
      if (cdTasksList) {
        if (tasks.length === 0) {
          cdTasksList.innerHTML = '<div class="text-zinc-500 text-xs py-2">No tasks queued.</div>';
        } else {
          cdTasksList.innerHTML = tasks.map(t => `
            <div class="flex items-center justify-between p-2 rounded-xl bg-white/[0.02] border border-white/5 text-[11px]">
              <div class="flex items-center gap-2">
                <span class="w-2 h-2 rounded-full ${t.status === 'published' ? 'bg-emerald-400' : 'bg-amber-400'}"></span>
                <span class="text-white font-mono uppercase">${escapeHtml(t.platform || 'meta')}</span>
              </div>
              <span class="text-zinc-400 font-mono text-[10px]">${escapeHtml(t.status || 'pending')}</span>
            </div>
          `).join('');
        }
      }
    }

    function closeCampaignDetailsModal() {
      const modal = document.getElementById('campaignDetailsModal');
      if (modal) modal.classList.add('hidden');
    }

    async function dispatchCampaignImmediate() {
      const btn = document.getElementById('btnDispatchCampaign');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Publishing...';
      }
      try {
        const res = await fetch('/publish/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            publish_now: true,
            immediate: true,
            campaign_id: currentCampaignId || 'demo-campaign',
            content_draft_ids: []
          })
        });
        if (res.ok) {
          showToast('Live post dispatched successfully!', true);
          if (currentCampaignId) {
            await openCampaignDetailsModal(currentCampaignId);
          }
        } else {
          showToast('Dispatch failed', false);
        }
      } catch (err) {
        showToast('Dispatch error: ' + err.message, false);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '<span class="material-symbols-outlined text-sm">rocket_launch</span><span>Publish Next Post</span>';
        }
      }
    }

    function writeMoreForCurrentCampaign() {
      closeCampaignDetailsModal();
      const campTitle = document.getElementById('cdTitle');
      const name = campTitle ? campTitle.textContent : 'current campaign';
      fillAndSend(`Generate creative copy and high-engagement visuals for campaign: ${name}`);
    }

    async function handleCreateCampaignIntent(prompt) {
      openNewCampaignModal();
      document.getElementById('modalCampName').value = prompt.replace(/create|launch|campaign/gi, '').trim() || 'New Autonomous Launch';
    }

    function triggerQuickTool(tool) {
      if (tool === 'analytics') {
        document.getElementById('emptyHeroState').classList.add('hidden');
        document.getElementById('messagesContainer').classList.remove('hidden');
        appendUserMessage('Run growth audit and display Campaign Analytics report', null);
        const tid = 'tid-' + Date.now();
        appendThinkingIndicator(tid);
        setTimeout(async () => {
          removeThinkingIndicator(tid);
          await handleAnalyticsIntent();
          scrollToBottom();
        }, 500);
      }
    }

    async function handleAnalyticsIntent() {
      try {
        const res = await fetch('/analytics/report?period_days=30');
        const report = await res.json();
        const msgArea = document.getElementById('messagesContainer');
        if (!msgArea) return;

        const card = document.createElement('div');
        card.className = 'space-y-4 animate-fade-in';
        const totals = report.totals || {};
        const impressions = totals.impressions != null ? Number(totals.impressions).toLocaleString() : '415,000';
        const clicks = totals.clicks != null ? Number(totals.clicks).toLocaleString() : '18,770';
        const spend = totals.spend != null ? ('$' + Number(totals.spend).toLocaleString()) : '$3,240';
        const ctr = totals.ctr != null ? totals.ctr : '4.52%';

        card.innerHTML = `
          <div class="flex items-center gap-2 text-xs text-zinc-400">
            <div class="w-6 h-6 rounded-lg bg-gradient-to-tr from-indigo-500 to-purple-500 flex items-center justify-center text-white">
              <span class="material-symbols-outlined text-sm">analytics</span>
            </div>
            <span class="text-white font-medium">OmniFlow Analytics Agent</span>
            <span>•</span>
            <span class="text-indigo-400 font-mono text-[11px]">GET /analytics/report · Live Audit</span>
          </div>

          <div class="glass-panel rounded-3xl p-6 space-y-5 border border-white/[0.08] shadow-2xl bg-[#0D1017]/90">
            <div class="flex items-center justify-between">
              <div>
                <h3 class="text-base font-bold text-white">Campaign Performance & ROI Audit</h3>
                <p class="text-xs text-zinc-400 mt-0.5">Aggregated cross-platform intelligence (Past 30 Days)</p>
              </div>
              <span class="px-2.5 py-0.5 rounded-full text-xs font-mono bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">Live Data</span>
            </div>

            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <span class="text-zinc-500 block text-[10px] uppercase font-medium">Total Impressions</span>
                <span class="text-lg font-extrabold text-white font-mono">${impressions}</span>
                <span class="text-[10px] text-emerald-400 font-mono block">▲ +18.4%</span>
              </div>
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <span class="text-zinc-500 block text-[10px] uppercase font-medium">Total Clicks</span>
                <span class="text-lg font-extrabold text-indigo-300 font-mono">${clicks}</span>
                <span class="text-[10px] text-zinc-400 font-mono block">CTR: ${ctr}</span>
              </div>
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <span class="text-zinc-500 block text-[10px] uppercase font-medium">Ad Spend</span>
                <span class="text-lg font-extrabold text-amber-300 font-mono">${spend}</span>
                <span class="text-[10px] text-zinc-400 font-mono block">Optimized</span>
              </div>
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <span class="text-zinc-500 block text-[10px] uppercase font-medium">Avg ROAS</span>
                <span class="text-lg font-extrabold text-emerald-400 font-mono">3.85x</span>
                <span class="text-[10px] text-emerald-400 font-mono block">High Intent</span>
              </div>
            </div>

            ${(report.recommendations && report.recommendations.length > 0) ? `
              <div class="p-3.5 rounded-2xl bg-white/[0.02] border border-white/5 space-y-2">
                <span class="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
                  <span class="material-symbols-outlined text-sm text-indigo-400">lightbulb</span>
                  Strategic AI Recommendations:
                </span>
                <div class="space-y-1 text-xs text-zinc-400">
                  ${report.recommendations.map(r => `
                    <div class="flex items-start gap-2">
                      <span class="material-symbols-outlined text-xs text-emerald-400 mt-0.5">check_circle</span>
                      <span>${escapeHtml(r)}</span>
                    </div>
                  `).join('')}
                </div>
              </div>
            ` : ''}
          </div>
        `;
        msgArea.appendChild(card);
        scrollToBottom();
      } catch (err) {
        showToast('Failed to load analytics: ' + err.message, false);
      }
    }

    
    // =========================================================================
    // INTENT 4: AI /BOOST TURBO MODE (POST /boost & /campaign/boost)
    // =========================================================================
    function triggerBoostMode() {
      document.getElementById('emptyHeroState').classList.add('hidden');
      document.getElementById('messagesContainer').classList.remove('hidden');
      appendUserMessage('/boost — Overclock multi-agent swarm performance', null);
      const tid = 'tid-' + Date.now();
      appendThinkingIndicator(tid);
      setTimeout(async () => {
        removeThinkingIndicator(tid);
        await handleBoostIntent('/boost');
        scrollToBottom();
      }, 600);
    }

    async function handleBoostIntent(prompt) {
      try {
        const res = await fetch('/campaign/boost');
        const data = await res.json();

        const msgArea = document.getElementById('messagesContainer');
        const card = document.createElement('div');
        card.className = 'space-y-4 animate-fade-in';
        card.innerHTML = `
          <div class="flex items-center gap-2 text-xs text-zinc-400">
            <div class="w-6 h-6 rounded-lg bg-gradient-to-tr from-amber-500 via-orange-500 to-indigo-500 flex items-center justify-center text-white shadow-md">
              <span class="material-symbols-outlined text-sm font-bold">bolt</span>
            </div>
            <span class="text-white font-medium">OmniFlow Boost Engine</span>
            <span>•</span>
            <span class="text-amber-400 font-mono text-[11px]">POST /campaign/boost · OVERCLOCK ACTIVE</span>
          </div>

          <div class="glass-panel rounded-3xl p-6 space-y-5 border border-amber-500/20 shadow-2xl relative overflow-hidden group">
            <!-- Ambient Card Backglow -->
            <div class="absolute -right-20 -top-20 w-72 h-72 bg-amber-500/10 rounded-full blur-3xl pointer-events-none"></div>

            <!-- Master Boost Visual Graphic Banner -->
            <div class="relative rounded-2xl overflow-hidden border border-white/10 shadow-2xl group/banner cursor-pointer" onclick="openBoostModal()">
              <img src="/static/assets/logos/omniflow_boost.svg" alt="OmniFlow /boost" class="w-full h-auto object-cover transform group-hover/banner:scale-[1.01] transition-transform duration-500"/>
              <div class="absolute bottom-3 right-3 px-3 py-1 rounded-lg bg-black/70 backdrop-blur-md border border-white/10 text-[11px] font-mono text-amber-300 flex items-center gap-1.5 opacity-90 group-hover/banner:opacity-100 transition">
                <span class="material-symbols-outlined text-xs">zoom_in</span>
                <span>Click to Expand Artwork</span>
              </div>
            </div>

            <!-- Telemetry Header -->
            <div class="flex flex-wrap items-center justify-between gap-2 pt-1 border-b border-white/[0.08] pb-4">
              <div>
                <h3 class="text-lg font-bold text-white flex items-center gap-2">
                  <span>Autonomous Swarm Turbocharged</span>
                  <span class="text-[10px] font-mono px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/40">10X Overclock</span>
                </h3>
                <p class="text-xs text-zinc-400 mt-1">Target Campaign: <span class="text-zinc-200 font-medium">${escapeHtml(data.campaign_name || 'Global Launch')}</span></p>
              </div>
              <div class="flex items-center gap-2">
                <span class="text-xs font-mono px-2.5 py-1 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 font-bold">
                  Target ROAS: ${data.boost_multiplier || '4.82x'}
                </span>
                <span class="text-xs font-mono px-2.5 py-1 rounded-full bg-indigo-500/15 text-indigo-300 border border-indigo-500/30">
                  Lift: ${data.lift_percentage || '+174%'}
                </span>
              </div>
            </div>

            <!-- 4-Stat Overclock Grid -->
            <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <div class="text-[11px] text-zinc-400">Boosted Reach</div>
                <div class="text-lg font-extrabold text-white font-mono">${data.projected_reach || '2,450,000'}</div>
                <div class="text-[10px] text-emerald-400">▲ +1.55M Incremental</div>
              </div>
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <div class="text-[11px] text-zinc-400">Swarm Concurrency</div>
                <div class="text-lg font-extrabold text-amber-300 font-mono">7 Micro-Agents</div>
                <div class="text-[10px] text-zinc-400">Parallel Execution</div>
              </div>
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <div class="text-[11px] text-zinc-400">Target ROAS</div>
                <div class="text-lg font-extrabold text-emerald-400 font-mono">${data.boost_multiplier || '4.82x'}</div>
                <div class="text-[10px] text-zinc-400">Baseline: ${data.baseline_roas || '2.20x'}</div>
              </div>
              <div class="p-3.5 rounded-2xl bg-black/40 border border-white/[0.08] space-y-1">
                <div class="text-[11px] text-zinc-400">Swarm Latency</div>
                <div class="text-lg font-extrabold text-indigo-300 font-mono">12 ms</div>
                <div class="text-[10px] text-emerald-400">Zero Bottlenecks</div>
              </div>
            </div>

            <!-- Active Optimization Callouts -->
            <div class="p-4 rounded-2xl bg-white/[0.02] border border-white/[0.06] space-y-2.5">
              <div class="text-xs font-semibold text-zinc-300 flex items-center gap-1.5">
                <span class="material-symbols-outlined text-sm text-amber-400">tune</span>
                <span>Active Swarm Optimizations Dispatched:</span>
              </div>
              <div class="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs text-zinc-400">
                ${(data.optimizations || [
                  'Overclocked Meta and TikTok ad budget allocation (+35% share shift)',
                  'Applied neuro-linguistic hook refinement for higher CTR conversion',
                  'Enabled parallelized multi-lingual queue for APAC & Western ecosystems',
                  'Swarm agent latency reduced to 12ms via distributed edge cache'
                ]).map(opt => `
                  <div class="flex items-start gap-2 p-2 rounded-xl bg-white/[0.02] border border-white/5">
                    <span class="material-symbols-outlined text-xs text-emerald-400 mt-0.5 shrink-0">check_circle</span>
                    <span class="text-[11px] leading-relaxed">${escapeHtml(opt)}</span>
                  </div>
                `).join('')}
              </div>
            </div>

            <!-- Action Bar -->
            <div class="flex flex-wrap items-center justify-between gap-3 pt-2">
              <div class="flex items-center gap-2">
                <button onclick="showToast('Turbocharged dispatch triggered across 7 platform feeds!')" class="px-5 py-2 rounded-full bg-gradient-to-r from-amber-500 to-orange-600 hover:from-amber-400 hover:to-orange-500 text-neutral-950 text-xs font-bold shadow-lg shadow-amber-500/20 flex items-center gap-2 transition active:scale-95">
                  <span class="material-symbols-outlined text-base">rocket_launch</span>
                  <span>Deploy Boost to All Channels</span>
                </button>
                <button onclick="fillAndSend('Generate viral video hooks for TikTok and Douyin under boost mode')" class="px-4 py-2 rounded-full bg-white/[0.06] hover:bg-white/[0.12] border border-white/10 text-white text-xs font-medium transition flex items-center gap-1.5">
                  <span class="material-symbols-outlined text-sm text-indigo-400">auto_fix_high</span>
                  <span>Synthesize Viral Hooks</span>
                </button>
              </div>
              <button onclick="openBoostModal()" class="text-xs text-amber-400 hover:text-amber-300 flex items-center gap-1 font-mono">
                <span>View Full-Res Boost Graphic</span>
                <span class="material-symbols-outlined text-sm">open_in_new</span>
              </button>
            </div>
          </div>
        `;
        msgArea.appendChild(card);
      } catch (err) {
        showToast('Error executing boost mode: ' + err.message, false);
      }
    }

    function openBoostModal() {
      document.getElementById('boostModal').classList.remove('hidden');
    }

    function closeBoostModal() {
      document.getElementById('boostModal').classList.add('hidden');
    }

    // =========================================================================
    // MCP API GATEWAY & LIVE INTEGRATIONS
    // =========================================================================
    async function loadMcpSettings() {
      try {
        const res = await fetch('/settings/apis');
        if (!res.ok) return;
        const data = await res.json();
        const masked = data.masked || {};
        const raw = data.raw || {};

        const metaIn = document.getElementById('inputMetaToken');
        const ttIn = document.getElementById('inputTikTokKey');
        const xhsIn = document.getElementById('inputXhsKey');
        const wxAppId = document.getElementById('inputWechatAppId');
        const wxSec = document.getElementById('inputWechatSecret');
        const stitchIn = document.getElementById('inputStitchUrl');
        const openAiIn = document.getElementById('inputOpenAiKey');

        if (metaIn && (masked.meta_access_token || raw.meta_access_token)) metaIn.value = masked.meta_access_token || raw.meta_access_token;
        if (ttIn && (masked.tiktok_api_key || raw.tiktok_api_key)) ttIn.value = masked.tiktok_api_key || raw.tiktok_api_key;
        if (xhsIn && (masked.xiaohongshu_api_key || raw.xiaohongshu_api_key)) xhsIn.value = masked.xiaohongshu_api_key || raw.xiaohongshu_api_key;
        if (wxAppId && raw.wechat_app_id) wxAppId.value = raw.wechat_app_id;
        if (wxSec && (masked.wechat_app_secret || raw.wechat_app_secret)) wxSec.value = masked.wechat_app_secret || raw.wechat_app_secret;
        if (stitchIn && raw.stitch_mcp_url) stitchIn.value = raw.stitch_mcp_url;
        if (openAiIn && (masked.openai_api_key || raw.openai_api_key)) openAiIn.value = masked.openai_api_key || raw.openai_api_key;
      } catch (e) {
        console.warn('Error loading MCP settings:', e);
      }
    }

    async function saveMcpSettings() {
      const btn = document.getElementById('btnSaveMcp');
      const feedback = document.getElementById('mcpSaveFeedback');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Saving...';
      }

      const payload = {};
      const metaVal = document.getElementById('inputMetaToken')?.value.trim();
      const ttVal = document.getElementById('inputTikTokKey')?.value.trim();
      const xhsVal = document.getElementById('inputXhsKey')?.value.trim();
      const wxIdVal = document.getElementById('inputWechatAppId')?.value.trim();
      const wxSecVal = document.getElementById('inputWechatSecret')?.value.trim();
      const stitchVal = document.getElementById('inputStitchUrl')?.value.trim();
      const openAiVal = document.getElementById('inputOpenAiKey')?.value.trim();

      if (metaVal) payload.meta_access_token = metaVal;
      if (ttVal) payload.tiktok_api_key = ttVal;
      if (xhsVal) payload.xiaohongshu_api_key = xhsVal;
      if (wxIdVal) payload.wechat_app_id = wxIdVal;
      if (wxSecVal) payload.wechat_app_secret = wxSecVal;
      if (stitchVal) payload.stitch_mcp_url = stitchVal;
      if (openAiVal) payload.openai_api_key = openAiVal;

      try {
        const res = await fetch('/settings/apis', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (res.ok) {
          if (feedback) feedback.textContent = 'Credentials saved and persisted!';
          showToast('API credentials saved successfully!', true);
          setTimeout(() => { if (feedback) feedback.textContent = ''; }, 3000);
        } else {
          showToast('Failed to save credentials', false);
        }
      } catch (err) {
        showToast('Error saving settings: ' + err.message, false);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '<span>Save & Apply Credentials</span>';
        }
      }
    }

    async function testMcpConnection() {
      const btn = document.getElementById('btnTestMcp');
      const statusMsg = document.getElementById('mcpTestStatusMsg');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-outlined text-xs animate-spin">sync</span><span>Testing...</span>';
      }
      try {
        const res = await fetch('/settings/apis/test', { method: 'POST' });
        if (res.ok) {
          const data = await res.json();
          if (statusMsg) {
            statusMsg.textContent = `Operational · ${(data.active_services || []).length} Services Ready · ${data.latency_ms || 18}ms`;
          }
          showToast(`Connected! Active: ${(data.active_services || []).join(', ')}`, true);
        } else {
          showToast('MCP test failed', false);
        }
      } catch (err) {
        showToast('Connection test error: ' + err.message, false);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = '<span class="material-symbols-outlined text-xs">sync</span><span>Test Connection</span>';
        }
      }
    }

    // =========================================================================
    // SCHEDULES & REAL-TIME DISPATCH QUEUE
    // =========================================================================
    async function loadSchedulesList() {
      const listEl = document.getElementById('schedulesModalList');
      const countBadge = document.getElementById('scheduledBadgeCount');
      try {
        const res = await fetch('/publish/tasks');
        if (!res.ok) return;
        const tasks = await res.json();
        if (countBadge) countBadge.textContent = tasks.length;
        if (!listEl) return;

        if (!tasks || tasks.length === 0) {
          listEl.innerHTML = '<div class="text-center py-6 text-zinc-500 text-xs">No publishing tasks scheduled. Create a campaign or approve a creative draft to schedule posts.</div>';
          return;
        }

        listEl.innerHTML = tasks.map(t => {
          const isPub = t.status === 'published';
          const timeStr = t.scheduled_at ? new Date(t.scheduled_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : (isPub ? 'Published' : 'Immediate');
          return `
            <div class="flex items-center justify-between p-3 rounded-2xl bg-white/[0.02] border border-white/5 hover:border-white/10 transition">
              <div class="flex items-center gap-3">
                <div class="w-8 h-8 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center font-mono text-[10px] text-zinc-300 uppercase">
                  ${escapeHtml((t.platform || 'meta').slice(0, 3))}
                </div>
                <div>
                  <div class="text-xs font-semibold text-white flex items-center gap-2">
                    <span>Task ${t.id ? t.id.substring(0, 8) : ''}</span>
                    <span class="text-[10px] px-2 py-0.2 rounded-full font-mono ${isPub ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30' : 'bg-amber-500/15 text-amber-300 border border-amber-500/30'}">
                      ${isPub ? 'Published 🟢' : 'Queued ⏳'}
                    </span>
                  </div>
                  <div class="text-[10px] text-zinc-400 mt-0.5">
                    Channel: <span class="text-zinc-200 capitalize">${escapeHtml(t.platform || 'meta')}</span> · Target: ${timeStr}
                  </div>
                </div>
              </div>
              ${!isPub ? `
                <button onclick="publishSingleTaskNow('${t.id}')" class="px-3 py-1 rounded-lg bg-indigo-600/30 hover:bg-indigo-600/50 border border-indigo-500/40 text-indigo-300 text-[11px] font-semibold transition">
                  Dispatch Now
                </button>
              ` : `
                <span class="text-[11px] text-emerald-400 flex items-center gap-1 font-mono">
                  <span class="material-symbols-outlined text-xs">verified</span> Live
                </span>
              `}
            </div>
          `;
        }).join('');
      } catch (err) {
        if (listEl) listEl.innerHTML = `<div class="text-rose-400 text-xs py-4">Error loading tasks: ${err.message}</div>`;
      }
    }

    async function triggerImmediatePublishAll() {
      try {
        const res = await fetch('/publish/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            publish_now: true,
            immediate: true,
            campaign_id: currentCampaignId || 'direct-approval',
            content_draft_ids: []
          })
        });
        if (res.ok) {
          showToast('Dispatched next task in queue!', true);
          loadSchedulesList();
        } else {
          showToast('Queue empty or dispatch error', false);
        }
      } catch (err) {
        showToast('Dispatch error: ' + err.message, false);
      }
    }

    async function publishSingleTaskNow(taskId) {
      try {
        const res = await fetch('/publish/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            publish_now: true,
            immediate: true,
            task_id: taskId,
            content_draft_ids: []
          })
        });
        if (res.ok) {
          showToast('Task dispatched to live feed!', true);
          loadSchedulesList();
        }
      } catch (e) {
        showToast('Error: ' + e.message, false);
      }
    }

    async function triggerDemoSeed() {
      showToast('Live syncing data from multi-agent engine...');
      try {
        const res = await fetch('/demo/seed', { method: 'POST' });
        if (res.ok) {
          const data = await res.json();
          showToast(`Synced! ${data.drafts_count || 0} drafts, ${data.tasks_count || 0} tasks ready.`, true);
          await loadCampaigns();
          await loadSchedulesList();
        } else {
          showToast('Failed to sync demo data', false);
        }
      } catch (err) {
        showToast('Sync error: ' + err.message, false);
      }
    }

    
    // =========================================================================
    // THEME ENGINE (Dark / Light Cupertino Mode)
    // =========================================================================
    function initTheme() {
      let saved = 'dark';
      try {
        saved = localStorage.getItem('omniflow_theme') || 'dark';
      } catch (_) {}
      applyTheme(saved);
    }

    function toggleTheme() {
      const isDark = document.documentElement.classList.contains('dark');
      const nextTheme = isDark ? 'light' : 'dark';
      applyTheme(nextTheme);
      try {
        localStorage.setItem('omniflow_theme', nextTheme);
      } catch (_) {}
    }

    function applyTheme(theme) {
      const icon = document.getElementById('themeIcon');
      if (theme === 'light') {
        document.documentElement.classList.remove('dark');
        document.documentElement.classList.add('light');
        if (icon) icon.textContent = 'dark_mode';
      } else {
        document.documentElement.classList.remove('light');
        document.documentElement.classList.add('dark');
        if (icon) icon.textContent = 'light_mode';
      }
    }

    // =========================================================================
    // USER AUTHENTICATION & PROFILE SWITCHER
    // =========================================================================
    let authMode = 'login'; // 'login' or 'register'

    async function checkAuthSession() {
      try {
        let token = null;
        try {
          token = localStorage.getItem('omniflow_auth_token');
        } catch (_) {}
        const res = await fetch('/auth/me', {
          headers: token ? { 'Authorization': `Bearer ${token}` } : {}
        });
        if (res.ok) {
          currentUser = await res.json();
          updateSidebarUserProfile(currentUser);
          closeAuthModal();
        }
      } catch (err) {
        console.warn('Could not load session:', err);
      }
    }

    function updateSidebarUserProfile(user) {
      if (!user) return;
      try {
        const initials = (user.full_name || 'AR').split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2);
        const initEl = document.getElementById('sidebarUserInitials');
        if (initEl) initEl.textContent = initials;
        const nameEl = document.getElementById('sidebarUserName');
        if (nameEl) nameEl.textContent = user.full_name || 'Alex Rivera';
        const roleEl = document.getElementById('sidebarUserRole');
        if (roleEl) roleEl.textContent = user.company || user.role || 'Global Brand HQ';
        
        const emailEl = document.getElementById('dropdownUserEmail');
        const compEl = document.getElementById('dropdownUserCompany');
        if (emailEl) emailEl.textContent = user.email || 'admin@omniflow.ai';
        if (compEl) compEl.textContent = `Role: ${user.role || 'Brand Director'}`;
      } catch (e) {
        console.warn('Error updating sidebar profile:', e);
      }
    }

    function toggleUserDropdown() {
      const dd = document.getElementById('userDropdown');
      if (dd) dd.classList.toggle('hidden');
    }

    // Close dropdown on click outside
    document.addEventListener('click', (e) => {
      const dd = document.getElementById('userDropdown');
      if (!dd) return;
      if (!e.target.closest('#userDropdown') && !e.target.closest('[onclick="toggleUserDropdown()"]')) {
        dd.classList.add('hidden');
      }
    });

    function fillDemoCredentials() {
      const email = document.getElementById('authEmailInput');
      const pwd = document.getElementById('authPasswordInput');
      if (email) email.value = 'admin@omniflow.ai';
      if (pwd) pwd.value = 'admin123';
    }

    async function loginDemoNow() {
      fillDemoCredentials();
      await handleAuthSubmit();
    }

    function openAuthModal(mode = 'login') {
      authMode = mode;
      switchAuthTab(mode);
      if (mode === 'login') {
        fillDemoCredentials();
      }
      const dd = document.getElementById('userDropdown');
      if (dd) dd.classList.add('hidden');
      const modal = document.getElementById('authModal');
      if (modal) {
        modal.classList.remove('hidden');
        modal.classList.add('flex');
        modal.style.display = 'flex';
      }
    }

    function closeAuthModal() {
      const modal = document.getElementById('authModal');
      if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
        modal.style.display = 'none';
      }
    }

    function switchAuthTab(mode) {
      authMode = mode;
      const tabLogin = document.getElementById('authTabLogin');
      const tabReg = document.getElementById('authTabRegister');
      const nameField = document.getElementById('authNameField');
      const compField = document.getElementById('authCompanyField');
      const submitBtn = document.getElementById('authSubmitBtn');
      const title = document.getElementById('authModalTitle');
      const switchPrompt = document.getElementById('authSwitchPrompt');
      const switchBtn = document.getElementById('authSwitchBtn');
      const demoCard = document.getElementById('demoAccountCard');

      if (mode === 'register') {
        tabReg.className = 'flex-1 py-1.5 rounded-lg text-xs font-medium text-white bg-white/10 transition';
        tabLogin.className = 'flex-1 py-1.5 rounded-lg text-xs font-medium text-zinc-400 hover:text-white transition';
        nameField.classList.remove('hidden');
        compField.classList.remove('hidden');
        if (demoCard) demoCard.classList.add('hidden');
        submitBtn.textContent = 'Create Account & Launch';
        title.textContent = 'Create OmniFlow Account';
        switchPrompt.textContent = 'Already have an account?';
        switchBtn.textContent = 'Sign in here';
      } else {
        tabLogin.className = 'flex-1 py-1.5 rounded-lg text-xs font-medium text-white bg-white/10 transition';
        tabReg.className = 'flex-1 py-1.5 rounded-lg text-xs font-medium text-zinc-400 hover:text-white transition';
        nameField.classList.add('hidden');
        compField.classList.add('hidden');
        if (demoCard) demoCard.classList.remove('hidden');
        fillDemoCredentials();
        submitBtn.textContent = 'Sign In';
        title.textContent = 'Sign In to OmniFlow';
        switchPrompt.textContent = "Don't have an account?";
        switchBtn.textContent = 'Create one now';
      }
    }

    function toggleAuthMode() {
      switchAuthTab(authMode === 'login' ? 'register' : 'login');
    }

    async function handleAuthSubmit(e) {
      if (e && e.preventDefault) e.preventDefault();
      const emailEl = document.getElementById('authEmailInput');
      const pwdEl = document.getElementById('authPasswordInput');
      const email = (emailEl ? emailEl.value : '').trim() || 'admin@omniflow.ai';
      const password = (pwdEl ? pwdEl.value : '') || 'admin123';
      const submitBtn = document.getElementById('authSubmitBtn');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Authenticating...';
      }

      try {
        let res, data;
        if (authMode === 'register') {
          const nameEl = document.getElementById('authNameInput');
          const compEl = document.getElementById('authCompanyInput');
          const fullName = (nameEl ? nameEl.value.trim() : '') || 'Marketing Lead';
          const company = (compEl ? compEl.value.trim() : '') || 'Global Brand HQ';
          res = await fetch('/auth/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password, full_name: fullName, company })
          });
        } else {
          res = await fetch('/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password })
          });
        }

        data = await res.json();
        if (!res.ok) {
          showToast(data.detail || 'Authentication failed', false);
          return;
        }

        // Success
        localStorage.setItem('omniflow_auth_token', data.token);
        currentUser = data.user;
        updateSidebarUserProfile(currentUser);
        closeAuthModal();
        showToast(`Welcome, ${currentUser.full_name || 'Admin'}! Active session started.`, true);
      } catch (err) {
        showToast('Network error during authentication: ' + err.message, false);
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = authMode === 'register' ? 'Create Account & Launch' : 'Sign In';
        }
      }
    }

    async function handleLogout() {
      try {
        await fetch('/auth/logout', { method: 'POST' });
      } catch (e) {}
      localStorage.removeItem('omniflow_auth_token');
      showToast('Signed out of session');
      setTimeout(() => {
        location.reload();
      }, 500);
    }

    // =========================================================================
    // SOCIAL INTEGRATIONS & OAUTH MANAGEMENT
    // =========================================================================
    function openIntegrationsModal() {
      document.getElementById('integrationsModal').classList.remove('hidden');
      loadIntegrationsStatus();
    }

    function closeIntegrationsModal() {
      document.getElementById('integrationsModal').classList.add('hidden');
    }

    async function loadIntegrationsStatus() {
      const container = document.getElementById('integrationsListContainer');
      try {
        const res = await fetch('/integrations/status');
        const data = await res.json();

        container.innerHTML = Object.entries(data).map(([key, item]) => {
          cachedPlatforms[key] = item;
          const isConn = item.status === 'connected';
          return `
            <div class="flex items-center justify-between p-3.5 rounded-2xl bg-white/[0.02] border border-white/5 hover:border-white/10 transition">
              <div class="flex items-center gap-3">
                <img src="/static/assets/logos/${key === 'instagram' ? 'instagram.svg' : (key + '.svg')}" class="w-8 h-8 rounded-xl shrink-0" onerror="this.src='/static/assets/logos/omniflow_logo.svg'"/>
                <div>
                  <div class="text-xs font-semibold text-white flex items-center gap-2">
                    <span>${escapeHtml(item.name)}</span>
                    <span class="text-[10px] px-2 py-0.2 rounded-full font-mono ${isConn ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30' : 'bg-zinc-800 text-zinc-400'}">
                      ${isConn ? 'Connected 🟢' : 'Not Connected'}
                    </span>
                  </div>
                  <div class="text-[11px] text-zinc-400 mt-0.5">
                    ${isConn ? `Account: <span class="text-zinc-200 font-mono">${escapeHtml(item.account_name || item.account_id)}</span>` : 'Requires API Key / OAuth permission'}
                  </div>
                </div>
              </div>
              <div class="flex items-center gap-2">
                ${isConn ? `
                  <button onclick="disconnectChannel('${key}')" class="px-3 py-1 rounded-lg text-zinc-400 hover:text-rose-400 hover:bg-rose-500/10 border border-white/5 text-[11px] transition">
                    Disconnect
                  </button>
                ` : `
                  <button onclick="promptConnectChannel('${key}')" class="px-3 py-1 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-[11px] font-medium shadow transition">
                    Connect
                  </button>
                `}
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        container.innerHTML = `<div class="text-rose-400 text-xs text-center py-4">Error loading integrations: ${err.message}</div>`;
      }
    }

    function promptConnectChannel(platform, platformName) {
      const sheet = document.getElementById('connectFormSheet');
      if (!sheet) return;
      sheet.classList.remove('hidden');
      const item = cachedPlatforms[platform];
      const name = platformName || (item ? item.name : platform.toUpperCase());
      const keyEl = document.getElementById('connectPlatformKey');
      if (keyEl) keyEl.value = platform;
      const titleEl = document.getElementById('connectSheetTitle');
      if (titleEl) {
        titleEl.innerHTML = `
          <span class="w-2 h-2 rounded-full bg-indigo-400"></span>
          <span>Connect ${escapeHtml(name)}</span>
        `;
      }
      const accNameEl = document.getElementById('connectAccountName');
      if (accNameEl) accNameEl.value = `Official ${name} Brand`;
      const accIdEl = document.getElementById('connectAccountId');
      if (accIdEl) accIdEl.value = `${platform}-act-live`;
      const accTokenEl = document.getElementById('connectAccessToken');
      if (accTokenEl) accTokenEl.value = '';
    }

    async function submitConnectChannel() {
      const platform = document.getElementById('connectPlatformKey').value;
      const account_name = document.getElementById('connectAccountName').value.trim() || 'Brand Official';
      const account_id = document.getElementById('connectAccountId').value.trim() || `${platform}-act`;
      const access_token = document.getElementById('connectAccessToken').value.trim() || 'EAAB_live_token_mock';

      try {
        const res = await fetch('/integrations/connect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ platform, account_name, account_id, access_token })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Connected ${platform.toUpperCase()} account successfully!`);
          document.getElementById('connectFormSheet').classList.add('hidden');
          loadIntegrationsStatus();
        } else {
          showToast('Failed to connect channel', false);
        }
      } catch (err) {
        showToast('Error connecting channel: ' + err.message, false);
      }
    }

    async function disconnectChannel(platform) {
      if (!confirm(`Are you sure you want to disconnect ${platform}?`)) return;
      try {
        await fetch('/integrations/disconnect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ platform })
        });
        showToast(`Disconnected ${platform}`);
        loadIntegrationsStatus();
      } catch (err) {
        showToast('Error disconnecting: ' + err.message, false);
      }
    }

    function scrollToBottom() {
      const el = document.getElementById('chatScrollArea');
      el.scrollTop = el.scrollHeight;
    }

    function escapeHtml(str) {
      if (str == null) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    // =========================================================================
    // FACEBOOK TOKEN MANAGER JAVASCRIPT
    // =========================================================================
    let activeCardIdForFbToken = null;

    async function openFacebookTokenModal(cardId) {
      if (cardId) activeCardIdForFbToken = cardId;
      const modal = document.getElementById('facebookTokenModal');
      if (!modal) return;
      modal.classList.remove('hidden');
      const errBox = document.getElementById('fbTokenErrorMsg');
      if (errBox) errBox.classList.add('hidden');
      await checkFacebookLiveStatus();
    }

    function closeFacebookTokenModal() {
      const modal = document.getElementById('facebookTokenModal');
      if (modal) modal.classList.add('hidden');
    }

    async function checkFacebookLiveStatus() {
      const dot = document.getElementById('fbTokenStatusDot');
      const title = document.getElementById('fbTokenStatusTitle');
      const desc = document.getElementById('fbTokenStatusDesc');
      const headerBadge = document.getElementById('headerFbBadge');

      try {
        const res = await fetch('/tools/facebook/status');
        const data = await res.json();
        const pageName = data.page_name || 'Mai boovoo';
        const pageId = data.page_id || '101728504668130';

        const nameEl = document.getElementById('fbModalPageName');
        if (nameEl) nameEl.textContent = pageName;
        const idDisplay = document.getElementById('fbModalPageIdDisplay');
        if (idDisplay) idDisplay.textContent = pageId;
        const pageIdInput = document.getElementById('fbInputPageId');
        if (pageIdInput && !pageIdInput.value) pageIdInput.value = pageId;

        if (data.connected) {
          if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400';
          if (title) title.textContent = `Connected to ${pageName} 🟢`;
          if (desc) desc.textContent = 'Token verified · Live photo & feed dispatch active';
          if (headerBadge) headerBadge.textContent = `Facebook: ${pageName} 🟢`;
        } else if (data.token_expired) {
          if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse';
          if (title) title.textContent = 'Session Token Expired 🟡';
          if (desc) desc.textContent = data.error || 'Temporary session token has expired. Paste a new token below.';
          if (headerBadge) headerBadge.textContent = 'Facebook: Token Expired 🟡';
        } else {
          if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-rose-400';
          if (title) title.textContent = 'Not Connected';
          if (desc) desc.textContent = data.error || 'Check credentials';
          if (headerBadge) headerBadge.textContent = 'Facebook: Connect';
        }
      } catch (err) {
        if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-rose-400';
        if (title) title.textContent = 'Connection Error';
        if (desc) desc.textContent = err.message;
      }
    }

    async function saveFacebookToken() {
      const saveBtn = document.getElementById('fbSaveTokenBtn');
      const tokenInput = document.getElementById('fbInputAccessToken');
      const pageIdInput = document.getElementById('fbInputPageId');
      const errBox = document.getElementById('fbTokenErrorMsg');

      const access_token = tokenInput.value.trim();
      const page_id = pageIdInput.value.trim() || '101728504668130';

      if (!access_token) {
        errBox.classList.remove('hidden');
        errBox.textContent = 'Please paste a valid Facebook Page Access Token.';
        return;
      }

      errBox.classList.add('hidden');
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">progress_activity</span> Verifying with Meta...';

      try {
        const res = await fetch('/tools/facebook/update-token', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ access_token, page_id })
        });
        const data = await res.json();

        if (res.ok && data.success) {
          showToast(`Facebook connected to ${data.page_name} 🟢!`, true);
          tokenInput.value = '';
          await checkFacebookLiveStatus();

          // Reset failed card button to allow immediate publish
          if (activeCardIdForFbToken) {
            const btn = document.getElementById(`${activeCardIdForFbToken}-approveBtn`);
            if (btn) {
              btn.className = 'px-5 py-2.5 rounded-full bg-gradient-to-r from-emerald-500 to-teal-600 hover:from-emerald-400 hover:to-teal-500 text-neutral-950 text-xs font-semibold shadow-lg shadow-emerald-500/25 flex items-center gap-2 transition active:scale-95';
              btn.innerHTML = '<span class="material-symbols-outlined text-base font-bold">check_circle</span><span>Publish to Mai boovoo Now 🟢</span>';
              btn.disabled = false;
              btn.onclick = () => approveAndPublish(activeCardIdForFbToken);
            }
            const updateBtn = document.getElementById(`${activeCardIdForFbToken}-updateTokenBtn`);
            if (updateBtn) updateBtn.remove();
          }

          setTimeout(() => {
            closeFacebookTokenModal();
          }, 1000);
        } else {
          errBox.classList.remove('hidden');
          errBox.textContent = data.detail || data.error || 'Failed to verify token with Meta Graph API.';
        }
      } catch (err) {
        errBox.classList.remove('hidden');
        errBox.textContent = 'Network error: ' + err.message;
      } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<span class="material-symbols-outlined text-sm">key</span><span>Verify & Save Token</span>';
      }
    }

    // Keyboard Shortcuts
    document.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'n') {
        e.preventDefault();
        resetToNewChat();
      }
    });

    // Explicit global window bindings to guarantee availability for HTML event handlers
    window.handleInputKey = handleInputKey;
    window.appendThinkingIndicator = appendThinkingIndicator;
    window._appendThinking = appendThinkingIndicator;
    window.removeThinkingIndicator = removeThinkingIndicator;
    window._removeThinking = removeThinkingIndicator;
    window.appendAiErrorMessage = appendAiErrorMessage;
    window.resetToNewChat = resetToNewChat;
    window._resetChat = resetToNewChat;
    window.submitChat = submitChat;
    window.loadCampaigns = loadCampaigns;
    window._loadCampaigns = loadCampaigns;
    window.renderDynamicCreativeCard = renderDynamicCreativeCard;
    window.openAuthModal = openAuthModal;
    window.closeAuthModal = closeAuthModal;
    window.loginDemoNow = loginDemoNow;
    window.handleAuthSubmit = handleAuthSubmit;
    window.fillDemoCredentials = fillDemoCredentials;
    window.fillAndSend = fillAndSend;
    window.clearAttachedImage = clearAttachedImage;
    window.handleMediaSelect = handleMediaSelect;
    window.approveAndPublish = approveAndPublish;
    window.copyDraftText = copyDraftText;
    window.togglePlatformChip = togglePlatformChip;
    window.toggleSidebar = toggleSidebar;
    window.toggleTheme = toggleTheme;
    window.showToast = showToast;
    window.openBoostModal = openBoostModal;
    window.closeBoostModal = closeBoostModal;
    window.openNewCampaignModal = openNewCampaignModal;
    window.closeNewCampaignModal = closeNewCampaignModal;
    window.openMcpModal = openMcpModal;
    window.closeMcpModal = closeMcpModal;
    window.openSchedulesModal = openSchedulesModal;
    window.closeSchedulesModal = closeSchedulesModal;
    window.openIntegrationsModal = openIntegrationsModal;
    window.closeIntegrationsModal = closeIntegrationsModal;
    window.openCampaignDetailsModal = openCampaignDetailsModal;
    window.closeCampaignDetailsModal = closeCampaignDetailsModal;
    window.switchDynamicCardPlatform = switchDynamicCardPlatform;
    window.triggerBoostMode = triggerBoostMode;
    window.triggerDemoSeed = triggerDemoSeed;
    window.triggerImmediatePublishAll = triggerImmediatePublishAll;
    window.triggerQuickTool = triggerQuickTool;
    window.synthesizeDraftsForCampaign = synthesizeDraftsForCampaign;
    window.writeMoreForCurrentCampaign = writeMoreForCurrentCampaign;
    window.dispatchCampaignImmediate = dispatchCampaignImmediate;
    window.promptConnectChannel = promptConnectChannel;
    window.submitConnectChannel = submitConnectChannel;
    window.disconnectChannel = disconnectChannel;
    window.loadIntegrationsStatus = loadIntegrationsStatus;
    window.regenerateCardDraft = regenerateCardDraft;
    window.publishSingleTaskNow = publishSingleTaskNow;
    window.saveMcpSettings = saveMcpSettings;
    window.testMcpConnection = testMcpConnection;
    window.handleCampaignModalSubmit = handleCampaignModalSubmit;
    window.toggleAuthMode = toggleAuthMode;
    window.switchAuthTab = switchAuthTab;
    window.handleLogout = handleLogout;
    window.simulateVoice = simulateVoice;
    window.scrollToBottom = scrollToBottom;
    window.escapeHtml = escapeHtml;
    window.openFacebookTokenModal = openFacebookTokenModal;
    window.closeFacebookTokenModal = closeFacebookTokenModal;
    window.checkFacebookLiveStatus = checkFacebookLiveStatus;
    window.saveFacebookToken = saveFacebookToken;

    // Boot: Start directly on Login Screen with Demo Account credentials ready
    document.addEventListener('DOMContentLoaded', () => {
      initTheme();
      openAuthModal('login');
      fillDemoCredentials();
      checkAuthSession();
      loadCampaigns();
      checkFacebookLiveStatus();
    });
  