
    // Early global bootstrap to prevent ReferenceErrors if handlers fire early
    window.handleInputKey = function(event) {
      if (!event && typeof window !== 'undefined') event = window.event;
      if (event && event.key === 'Enter' && !event.shiftKey) {
        if (event.preventDefault) event.preventDefault();
        if (typeof window.submitChat === 'function') window.submitChat();
      }
    };
    window.appendThinkingIndicator = function(id) {
      if (typeof window._appendThinking === 'function') return window._appendThinking(id);
      return id || ('thinking-' + Date.now());
    };
    window.removeThinkingIndicator = function(id) {
      if (typeof window._removeThinking === 'function') window._removeThinking(id);
    };
    window.loadCampaigns = function() {
      if (typeof window._loadCampaigns === 'function') return window._loadCampaigns();
    };
    window.resetToNewChat = function() {
      if (typeof window._resetChat === 'function') return window._resetChat();
    };
    window.triggerBoostMode = function() {
      if (typeof window._triggerBoostMode === 'function') return window._triggerBoostMode();
    };
    window.publishCurrentCreative = function(cardId, img, pid) {
      if (typeof window._publishCurrentCreative === 'function') return window._publishCurrentCreative(cardId, img, pid);
    };
  