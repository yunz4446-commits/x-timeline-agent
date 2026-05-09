"""Timeline scraper for X.com — browser-based."""
import logging, re, random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from dateutil.parser import isoparse
from .browser import XBrowser

logger = logging.getLogger(__name__)


@dataclass
class ScrapedTweet:
    tweet_id: str = ''
    author_username: str = ''
    author_display_name: str = ''
    text: str = ''
    lang: str = ''
    is_retweet: bool = False
    is_reply: bool = False
    reply_to_username: str = ''
    like_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    view_count: int = 0
    tweet_created_at: Optional[datetime] = None
    link_urls: list = field(default_factory=list)
    raw_text: str = ''


class TimelineScraper:
    def __init__(self, browser: XBrowser):
        self._browser = browser
        self._seen_ids = set()

    @staticmethod
    def _human_wait(page, min_ms, max_ms):
        """Random wait to simulate human reading/scanning pace."""
        page.wait_for_timeout(random.randint(min_ms, max_ms))

    def _extract_articles_js(self, page) -> dict:
        """Run JS evaluation to extract visible tweet article data from page.
        Returns {'data': [...], 'diag': {...}}."""
        articles_result = page.evaluate("""
                (() => {
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    const diag = {errors: [], socialContextSamples: [], noTextHasLink: 0,
                                  unlabeledSocialContext: 0, promotedSkipped: 0,
                                  retweetOk: 0, retweetNoNested: 0, retweetShortText: 0,
                                  pureRetweetFixed: 0, pureRetweetFailed: 0};
                    const data = Array.from(articles).map((el, idx) => {
                        try {
                            let tid = '';
                            // Primary: /status/{id} link
                            const statusLink = el.querySelector('a[href*="/status/"]');
                            if (statusLink) {
                                const href = statusLink.href;
                                const m = href.match(/\\/status\\/(\\d+)/);
                                if (m) { tid = m[1]; }
                            }
                            // Fallback: <time> parent <a> often wraps /status/{id}
                            if (!tid) {
                                const tm = el.querySelector('time');
                                if (tm) {
                                    const timeLink = tm.closest('a');
                                    if (timeLink) {
                                        const href = timeLink.getAttribute('href') || '';
                                        const m = href.match(/\\/status\\/(\\d+)/);
                                        if (m) { tid = m[1]; }
                                    }
                                }
                            }
                            // Fallback: X native Articles (/article/{id})
                            if (!tid) {
                                const articleLink = el.querySelector('a[href*="/article/"]');
                                if (articleLink) {
                                    const href = articleLink.getAttribute('href') || '';
                                    const m = href.match(/\\/article\\/(\\d+)/);
                                    if (m) { tid = 'a-' + m[1]; }
                                }
                            }
                            if (!tid) {
                                diag.skippedNoId = (diag.skippedNoId || 0) + 1;
                                if (diag.skippedNoId <= 3) {
                                    diag.errors.push('idx=' + idx + ' no tid, snippet=' + (el.innerText || '').trim().slice(0, 80));
                                }
                                return null;
                            }

                            // Skip promoted tweets. placementTracking is used by both
                            // ads AND media view tracking (video/image); skip only when
                            // it's NOT wrapped inside a tweetPhoto media container
                            const placementEl = el.querySelector('[data-testid="placementTracking"]');
                            if (placementEl && !placementEl.closest('[data-testid="tweetPhoto"]')) {
                                diag.promotedSkipped++; return null;
                            }
                            for (const span of el.querySelectorAll('span')) {
                                const txt = (span.textContent || '').trim();
                                if (/^(Promoted|Ad|推广|广告|広告|プロモーション|Sponsored)$/i.test(txt)) {
                                    diag.promotedSkipped++; return null;
                                }
                            }

                            let author = '';
                            for (const l of el.querySelectorAll('a[role="link"]')) {
                                const h = l.getAttribute('href') || '';
                                if (h.startsWith('/') && !h.includes('/status/') && !h.startsWith('/i/')) {
                                    author = h.replace('/', ''); break;
                                }
                            }

                            let displayName = '';
                            const un = el.querySelector('[data-testid="User-Name"] a');
                            if (un) displayName = (un.textContent || '').trim();

                            let text = '';
                            const tt = el.querySelector('[data-testid="tweetText"]');
                            if (tt) text = (tt.textContent || '').trim();
                            if (!text) {
                                const ld = el.querySelector('div[lang]');
                                if (ld) text = (ld.textContent || '').trim();
                            }
                            if (!text) {
                                const da = el.querySelector('div[dir="auto"]');
                                if (da) text = (da.textContent || '').trim();
                            }
                            if (!text) text = (el.innerText || '').trim();

                            let timeStr = '';
                            const tm = el.querySelector('time');
                            if (tm) timeStr = tm.getAttribute('datetime') || '';

                            let isRetweet = false;
                            const sc = el.querySelector('[data-testid="socialContext"]');
                            if (sc && /repost|转推|已转推|转帖|已转帖|转发|已转发|转发了|リツイート|リポスト/i.test(sc.textContent || '')) isRetweet = true;
                            // Fallback: some pure retweets have no socialContext or use different text;
                            // the article's first 200 chars of innerText typically mention the reposter
                            if (!isRetweet) {
                                const preview = (el.innerText || '').slice(0, 200);
                                if (/已转帖|已转推|转发|已转发|转发了|Reposted|has reposted|转推了|リツイートしました/i.test(preview)) isRetweet = true;
                            }

                            let isReply = false, replyTo = '';
                            if (sc && /replying|回复|回覆|返信/i.test(sc.textContent || '')) {
                                isReply = true;
                                const rm = (sc.textContent || '').match(/@([\\w_]+)/);
                                if (rm) replyTo = rm[1];
                            }
                            if (!isReply && text) {
                                const firstLine = text.split(/[\\n\\r]/)[0].trim();
                                const replyConvention = firstLine.match(/^@(\\w+)\\b/);
                                if (replyConvention) {
                                    isReply = true;
                                    replyTo = replyConvention[1];
                                }
                            }

                            // Pure retweet: the retweeted tweet's text lives in a
                            // nested <article> or deeper element, not the outer one.
                            // The fallback chain above (tweetText → div[lang] → dir=auto → innerText)
                            // only finds garbage metadata when there's no quote text.
                            // Also enter when text looks like wrapper-only junk
                            // (e.g. "Jeffrey 已转帖\\nGeoMetric\\n@GeoMetric_9\\n·\\n32分钟\\n1\\n2\\n32\\n658")
                            const wrapperPattern = /(已转帖|已转推|转发|已转发|转发了|Reposted|has reposted|转推了|リツイートしました|リツイート|リポスト)/i;
                            const looksLikeWrapper = wrapperPattern.test(text);
                            if (isRetweet && (!tt || !text || text.length < 10 || looksLikeWrapper)) {
                                const nestedArticles = el.querySelectorAll('article');
                                let foundInNested = false;

                                // Strategy 1: search nested <article> elements
                                for (const na of nestedArticles) {
                                    let nestedText = '';
                                    const ntt2 = na.querySelector('[data-testid="tweetText"]');
                                    if (ntt2) nestedText = (ntt2.textContent || '').trim();
                                    if (!nestedText) {
                                        const nld = na.querySelector('div[lang]');
                                        if (nld) nestedText = (nld.textContent || '').trim();
                                    }
                                    if (!nestedText) {
                                        const nda = na.querySelector('div[dir="auto"]');
                                        if (nda) nestedText = (nda.textContent || '').trim();
                                    }
                                    if (!nestedText) nestedText = (na.innerText || '').trim();
                                    // Must be substantially longer than wrapper junk, and not itself a wrapper
                                    if (nestedText.length > 30 && !wrapperPattern.test(nestedText.slice(0, 80))) {
                                        text = nestedText;
                                        foundInNested = true;
                                        for (const l of na.querySelectorAll('a[role="link"]')) {
                                            const h = l.getAttribute('href') || '';
                                            if (h.startsWith('/') && !h.includes('/status/') && !h.startsWith('/i/')) {
                                                author = h.replace('/', ''); break;
                                            }
                                        }
                                        const unNested = na.querySelector('[data-testid="User-Name"] a');
                                        if (unNested) displayName = (unNested.textContent || '').trim();
                                        break;
                                    }
                                }

                                if (!foundInNested && looksLikeWrapper) {
                                    // Strategy 2: try all [data-testid="tweetText"] at any depth
                                    const allTT = el.querySelectorAll('[data-testid="tweetText"]');
                                    for (const ttEl of allTT) {
                                        const ttText = (ttEl.textContent || '').trim();
                                        if (ttText.length > 30 && !wrapperPattern.test(ttText.slice(0, 80))) {
                                            text = ttText;
                                            foundInNested = true;
                                            break;
                                        }
                                    }
                                }

                                if (!foundInNested && looksLikeWrapper) {
                                    // Strategy 3: longest non-wrapper div[lang] or div[dir="auto"]
                                    let bestText = '';
                                    for (const d of el.querySelectorAll('div[lang], div[dir="auto"]')) {
                                        const dt = (d.textContent || '').trim();
                                        if (dt.length > bestText.length && !wrapperPattern.test(dt.slice(0, 80))) {
                                            bestText = dt;
                                        }
                                    }
                                    if (bestText.length > 30) {
                                        text = bestText;
                                        foundInNested = true;
                                    }
                                }

                                if (!foundInNested && looksLikeWrapper) {
                                    // Strategy 4: strip wrapper lines from innerText,
                                    // keep longest substantive line as content
                                    const fullText = (el.innerText || '').trim();
                                    const scEl = el.querySelector('[data-testid="socialContext"]');
                                    const scText = scEl ? (scEl.textContent || '').trim() : '';
                                    const lines = fullText.split('\\n').map(l => l.trim()).filter(l => {
                                        if (!l) return false;
                                        if (wrapperPattern.test(l)) return false;
                                        if (/^@[\\w_]+$/.test(l)) return false;
                                        if (/^(\\d{1,3}(,\\d{3})*|\\d+万?)$/.test(l)) return false;
                                        if (/^·$/.test(l)) return false;
                                        if (/^(\\d+\\s*(分钟|小时|秒|h|m|s)|\\d+h|\\d+m|\\d+s)/i.test(l)) return false;
                                        if (l === scText) return false;
                                        return l.length > 3;
                                    });
                                    if (lines.length > 0) {
                                        lines.sort((a, b) => b.length - a.length);
                                        text = lines[0];
                                        foundInNested = true;
                                    }
                                }

                                if (!foundInNested && nestedArticles.length === 0) {
                                    diag.retweetNoNested++;
                                }
                                if (foundInNested && looksLikeWrapper) {
                                    diag.pureRetweetFixed = (diag.pureRetweetFixed || 0) + 1;
                                } else if (!foundInNested && looksLikeWrapper) {
                                    diag.pureRetweetFailed = (diag.pureRetweetFailed || 0) + 1;
                                    text = '[纯转帖，无法提取原文]';
                                }
                                if (text && text.length >= 10) {
                                    diag.retweetOk++;
                                } else {
                                    diag.retweetShortText++;
                                }
                            }

                            if (sc) {
                                const scText = (sc.textContent || '').trim();
                                if (scText && diag.socialContextSamples.length < 20) {
                                    diag.socialContextSamples.push(scText);
                                }
                                if (scText && !/(repost|转推|已转推|转帖|已转帖|转发|已转发|转发了|リツイート|リポスト|replying|回复|回覆|返信)/i.test(scText)) {
                                    diag.unlabeledSocialContext++;
                                }
                            }

                            let likeCount = 0, retweetCount = 0, replyCount = 0;
                            for (const b of el.querySelectorAll('button[aria-label]')) {
                                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                                const nm = label.match(/([\\d,]+)/);
                                if (!nm) continue;
                                const n = parseInt(nm[1].replace(/,/g, ''));
                                if (/like|favorite/.test(label)) likeCount = n;
                                else if (/retweet|repost/.test(label)) retweetCount = n;
                                else if (/reply/.test(label)) replyCount = n;
                            }

                            const extLinks = [];
                            for (const l of el.querySelectorAll('a[href*="http"]')) {
                                const h = l.getAttribute('href') || '';
                                if (h.startsWith('https://') && !h.includes('x.com') && !h.includes('twitter.com')) {
                                    extLinks.push(h);
                                }
                            }

                            if ((!text || text.length < 5) && extLinks.length > 0) {
                                diag.noTextHasLink++;
                            }

                            return {tid, author, displayName, text, timeStr, isRetweet, isReply, replyTo,
                                    likeCount, retweetCount, replyCount, extLinks};
                        } catch(e) {
                            diag.errors.push('idx=' + idx + ' exception: ' + e.message);
                            diag.nullCount++;
                            return null;
                        }
                    }).filter(Boolean);
                    return {data: data, diag: diag};
                })();
""")
        return articles_result

    def scrape(self, max_scrolls=20, max_tweets=200):
        """Scrape timeline. Returns list of tweets, or None if not logged in."""
        page = self._browser.page
        current = page.url.lower()
        if 'x.com/home' not in current:
            logger.info('Navigating to x.com/home...')
            page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=60000)
        else:
            logger.info('Already on %s, reusing page', page.url)

        # Verify we are actually on x.com/home (not redirected to login/root)
        new_url = page.url.lower()
        if 'login' in new_url:
            logger.error('Redirected to login page — session expired')
            return None
        if 'x.com/home' not in new_url:
            logger.error('Not on home page, got %s — session may be expired', page.url)
            return None
        # X.com loads its timeline via JS (XHR) — wait with retries
        for retry in range(3):
            try:
                page.wait_for_selector('article[data-testid="tweet"] a[href*="/status/"]', timeout=12000)
                logger.info('Timeline rendered')
                break
            except Exception:
                if retry < 2:
                    logger.info('Timeline not ready (attempt %d/3), refreshing page...', retry + 1)
                    page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=30000)
                    self._dismiss_modals(page)
                    self._human_wait(page, 1500, 3000)
                else:
                    logger.warning('Timeline did not render after 3 attempts, proceeding anyway')
        self._human_wait(page, 1500, 3000)
        self._dismiss_modals(page)

        if not self._ensure_following_latest(page):
            logger.error('Could not switch to Following+Latest — scraping whatever is visible')

        count = page.locator('article[data-testid="tweet"]').count()
        logger.info('Visible tweet articles: %d', count)
        logger.info('Current URL: %s', page.url)
        tweets = []
        scrolls = 0
        stale = 0

        while scrolls < max_scrolls and len(tweets) < max_tweets and stale < 12:
            # Expand truncated long tweets via JS — stays within
            # article[data-testid="tweet"] scope so sidebar "Show more"
            # buttons are never touched. Uses regex for variant matching
            # plus dispatchEvent+click for React compatibility.
            expanded = page.evaluate("""
                (() => {
                    let count = 0;
                    const pattern = /^(显示更多|Show more|展开全文|もっと見る|さらに表示|Show this thread)/i;
                    document.querySelectorAll('article[data-testid="tweet"]').forEach(article => {
                        for (const el of article.querySelectorAll('span, button, div[role="button"]')) {
                            const t = (el.textContent || '').trim();
                            if (pattern.test(t)) {
                                el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                el.click();
                                count++;
                            }
                        }
                    });
                    return count;
                })();
            """)
            if expanded > 0:
                logger.info('Expanded %d tweets', expanded)
                self._human_wait(page, 400, 700)

            articles_result = self._extract_articles_js(page)
            articles_data = articles_result['data']
            diag = articles_result.get('diag', {})
            pw_article_count = page.locator('article[data-testid="tweet"]').count()
            logger.info('Extraction: JS=%d articles, PW=%d articles, errors=%d',
                        len(articles_data), pw_article_count, len(diag.get('errors', [])))
            # Log all article testids on page (catch non-standard content types)
            if scrolls == 0:
                all_testids = page.evaluate("""
                    (() => {
                        const counts = {};
                        document.querySelectorAll('article').forEach(el => {
                            const tid = el.getAttribute('data-testid') || '(none)';
                            counts[tid] = (counts[tid] || 0) + 1;
                        });
                        return counts;
                    })();
                """)
                logger.info('All article data-testids on page: %s', all_testids)
            # Per-tweet summary for debugging
            tweet_summary = [f"{d['tid'][:12]}|len={len(d.get('text','') or '')}|rt={1 if d.get('isRetweet') else 0}"
                           for d in articles_data[:8]]
            if tweet_summary:
                logger.info('Tweet samples: %s', ' ; '.join(tweet_summary))
            if diag.get('errors'):
                for err in diag['errors']:
                    logger.warning('JS extraction: %s', err)
            if diag.get('socialContextSamples'):
                logger.info('socialContext samples: %s', diag['socialContextSamples'])
            if diag.get('unlabeledSocialContext'):
                logger.info('unlabeled socialContext: %d', diag['unlabeledSocialContext'])
            if diag.get('noTextHasLink'):
                logger.info('link-only tweets (no text): %d', diag['noTextHasLink'])
            if diag.get('promotedSkipped'):
                logger.info('promoted tweets skipped: %d', diag['promotedSkipped'])
            if diag.get('skippedNoId'):
                logger.info('skipped (no tweet/article id): %d', diag['skippedNoId'])
            if diag.get('retweetOk') or diag.get('retweetNoNested') or diag.get('retweetShortText') or diag.get('pureRetweetFixed') or diag.get('pureRetweetFailed'):
                logger.info('retweet diag: ok=%d noNested=%d shortText=%d pureFixed=%d pureFailed=%d',
                            diag.get('retweetOk', 0), diag.get('retweetNoNested', 0),
                            diag.get('retweetShortText', 0),
                            diag.get('pureRetweetFixed', 0), diag.get('pureRetweetFailed', 0))
            new_count = 0
            for d in articles_data:
                tid = d['tid']
                if tid in self._seen_ids:
                    continue
                self._seen_ids.add(tid)
                tweet = self._js_dict_to_tweet(d)
                if tweet:
                    tweets.append(tweet)
                    new_count += 1
                    if len(tweets) >= max_tweets:
                        break

            if new_count == 0:
                stale += 1
            else:
                stale = 0

            # Scroll with jitter: vary target article index so the pattern
            # isn't identical every cycle. 80% last, 15% 2nd-last, 5% 3rd-last.
            offset_choices = [0] * 16 + [1, 1, 1, 2]  # ~80/15/5
            offset = offset_choices[random.randint(0, len(offset_choices) - 1)]
            page.evaluate(f"""
                (() => {{
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    if (articles.length > 0) {{
                        const idx = Math.max(0, articles.length - 1 - {offset});
                        articles[idx].scrollIntoView(false);
                    }}
                }})();
            """)
            # Random wait for XHR to load new content
            self._human_wait(page, 400, 900)
            # Quick content check — break early once new articles appear
            prev_count = page.locator('article[data-testid="tweet"]').count()
            for _ in range(6):
                page.wait_for_timeout(200)
                if page.locator('article[data-testid="tweet"]').count() > prev_count:
                    break
            # Occasional reading pause (~12% chance, 1.5-4s)
            if random.random() < 0.12:
                self._human_wait(page, 1500, 4000)
            # Occasional micro scroll-up (human overshoot correction, ~15% chance)
            if random.random() < 0.15:
                up_px = random.randint(80, 200)
                page.evaluate(f'window.scrollBy(0, -{up_px})')
                page.wait_for_timeout(random.randint(200, 400))
            scrolls += 1
            if scrolls % 5 == 0:
                logger.info('Scrolled %d, %d tweets', scrolls, len(tweets))

        retweets = sum(1 for t in tweets if t.is_retweet)
        replies = sum(1 for t in tweets if t.is_reply)
        logger.info('Timeline done: %d tweets (%d retweets, %d replies)', len(tweets), retweets, replies)
        return tweets

    def _js_dict_to_tweet(self, d: dict) -> Optional[ScrapedTweet]:
        """Convert a JS-extracted dict to a ScrapedTweet — time parsing in Python."""
        tweet = ScrapedTweet()
        tweet.tweet_id = d['tid']
        tweet.author_username = d.get('author', '')
        tweet.author_display_name = d.get('displayName', '')
        tweet.text = d.get('text', '')
        tweet.is_retweet = d.get('isRetweet', False)
        tweet.is_reply = d.get('isReply', False)
        tweet.reply_to_username = d.get('replyTo', '')
        tweet.like_count = d.get('likeCount', 0)
        tweet.retweet_count = d.get('retweetCount', 0)
        tweet.reply_count = d.get('replyCount', 0)
        tweet.link_urls = d.get('extLinks', [])
        tweet.raw_text = tweet.text

        # Parse time
        time_str = d.get('timeStr', '')
        if time_str:
            try:
                tweet.tweet_created_at = isoparse(time_str)
            except Exception:
                pass

        # Fallback: snowflake ID → timestamp
        if tweet.tweet_created_at is None and tweet.tweet_id:
            try:
                tid_int = int(tweet.tweet_id)
                tweet_epoch_ms = (tid_int >> 22) + 1288834974657
                tweet.tweet_created_at = datetime.fromtimestamp(
                    tweet_epoch_ms / 1000, tz=timezone.utc)
            except Exception:
                pass

        return tweet


    def _dump_tab_state(self, page, label: str = ""):
        """Log tab state — single-line summary."""
        try:
            tabs = page.locator('[data-testid="ScrollSnap-List"] [role="tab"]').all()
            if not tabs:
                tabs = page.locator('[role="tab"]').all()
            texts = []
            for tab in tabs[:5]:
                try:
                    txt = (tab.text_content() or '').strip()[:20]
                    sel = tab.get_attribute('aria-selected') or ''
                    texts.append(f'"{txt}"' + ('*' if sel == 'true' else ''))
                except Exception:
                    pass
            logger.info('Tabs [%s]: %s', label, ' | '.join(texts))
        except Exception:
            pass

    def _verify_timeline_is_chronological(self, page) -> bool:
        """Check if first few tweets have strictly descending timestamps."""
        try:
            times = page.evaluate("""
                (() => {
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    const times = [];
                    for (let i = 0; i < Math.min(articles.length, 5); i++) {
                        const timeEl = articles[i].querySelector('time');
                        if (timeEl) {
                            const dt = timeEl.getAttribute('datetime');
                            if (dt) times.push(new Date(dt).getTime());
                        }
                    }
                    return times;
                })();
            """)
            if len(times) < 3:
                logger.info('Not enough timestamps (%d) to verify chronological order', len(times))
                return False
            for i in range(len(times) - 1):
                if times[i] < times[i + 1]:
                    logger.info('Timeline NOT chronological: tweet[%d] older than tweet[%d]', i, i + 1)
                    return False
            logger.info('Timeline IS chronological: %d tweets in descending order', len(times))
            return True
        except Exception as exc:
            logger.warning('_verify_timeline_is_chronological failed: %s', exc)
            return False

    def _ensure_following_tab(self, page) -> bool:
        """Switch to the 'Following' tab. Returns True if already there or switched."""
        self._dump_tab_state(page, 'before_following')
        try:
            tab = page.locator('[data-testid="ScrollSnap-List"] [role="tab"]').filter(
                has_text=re.compile(r'Following|正在关注|フォロー中')).first
            if tab.count() == 0:
                tab = page.locator('[role="tab"]').filter(
                    has_text=re.compile(r'Following|正在关注|フォロー中')).first
            if tab.count() == 0:
                logger.error('Following tab not found')
                return False

            is_selected = tab.get_attribute('aria-selected')
            if is_selected == 'true':
                logger.info('Already on Following tab')
                return True

            logger.info('Clicking Following tab...')
            tab.click()
            page.wait_for_timeout(1500)
            try:
                page.wait_for_selector('[role="tab"][aria-selected="true"]', timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(500)

            is_selected = tab.get_attribute('aria-selected')
            self._dump_tab_state(page, 'after_following_click')
            if is_selected == 'true':
                logger.info('Following tab switch confirmed')
                return True
            logger.warning('Following tab switch UNCONFIRMED — aria-selected=%s', is_selected)
            return False
        except Exception as exc:
            logger.warning('_ensure_following_tab failed: %s', exc)
            return False

    def _ensure_latest_sort(self, page) -> bool:
        """Open the sort dropdown on the Following tab and select 'Latest'."""
        logger.info('Ensuring Latest sort...')
        try:
            following_tab = page.locator('[role="tab"][aria-selected="true"]').first
            if following_tab.count() == 0:
                logger.error('No active tab found for sort switch')
                return False

            expanded = following_tab.get_attribute('aria-expanded')
            logger.info('Following tab aria-expanded=%s', expanded)

            # Click the tab to open the dropdown (the entire tab is the toggle)
            logger.info('Clicking Following tab to open sort dropdown...')
            following_tab.click()
            page.wait_for_timeout(1000)

            # Check the sort dropdown menu items
            menu_debug = page.evaluate("""
                (() => {
                    const menu = document.querySelector('[role="menu"]');
                    if (!menu) return {found: false};
                    const items = menu.querySelectorAll('[role="menuitem"], [role="menuitemradio"]');
                    return {found: true, itemCount: items.length,
                            texts: Array.from(items).map(i => i.textContent.trim())};
                })();
            """)
            logger.info('Menu: found=%s count=%s items=%s',
                        menu_debug.get('found'), menu_debug.get('itemCount'), menu_debug.get('texts'))

            # Strategy: click the second menuitem (Latest is always second after Top)
            menu_item = None
            if menu_debug.get('found') and menu_debug.get('itemCount', 0) >= 2:
                items = page.locator('[role="menu"] [role="menuitem"], [role="menu"] [role="menuitemradio"]')
                count = items.count()
                logger.info('Found %d menu items via locator', count)
                if count >= 2:
                    # "Latest" is typically the second item
                    menu_item = items.nth(1)
                    logger.info('Selected second menuitem by position')

            # Fallback: text match
            if menu_item is None:
                menu_item = page.locator(
                    '[role="menu"] [role="menuitem"], [role="menu"] [role="menuitemradio"]'
                ).filter(has_text=re.compile(r'Latest|最新|最新推文|最新のツイート')).first

            # Fallback: JS click
            if menu_item is None or menu_item.count() == 0:
                clicked = page.evaluate("""
                    (() => {
                        const menu = document.querySelector('[role="menu"]');
                        if (!menu) return 'no-menu';
                        const items = menu.querySelectorAll('[role="menuitem"], [role="menuitemradio"]');
                        if (items.length >= 2) {
                            items[1].click();
                            return 'clicked-item[1]:' + items[1].textContent.trim();
                        }
                        return 'not-enough-items:' + items.length;
                    })();
                """)
                logger.info('JS click result: %s', clicked)
                if 'clicked-item' in str(clicked):
                    page.wait_for_timeout(1500)
                    try:
                        page.wait_for_selector('article[data-testid="tweet"]', timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                    if self._verify_timeline_is_chronological(page):
                        logger.info('Latest sort confirmed via JS index click')
                        return True

            if menu_item is None or menu_item.count() == 0:
                logger.error('"Latest" menu item not found after all strategies')
                page.keyboard.press('Escape')
                return False

            logger.info('Clicking "Latest/最近" menu item via JS...')
            # Use JS click which works more reliably with React event handlers
            click_result = page.evaluate("""
                (() => {
                    const items = document.querySelectorAll(
                        '[role="menu"] [role="menuitem"], [role="menu"] [role="menuitemradio"]');
                    if (items.length >= 2) {
                        const item = items[1];  // second item = 最近/最新/Latest
                        const txt = item.textContent.trim();
                        item.click();
                        // Also dispatch mousedown/mouseup for React
                        item.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        item.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        return 'clicked-item[1]:' + txt;
                    }
                    return 'not-enough-items:' + items.length;
                })();
            """)
            logger.info('JS click result: %s', click_result)

            # Wait for menu to close (indicates selection was made)
            try:
                page.wait_for_selector('[role="menu"]', state='detached', timeout=5000)
                logger.info('Menu closed after selection')
            except Exception:
                logger.warning('Menu did not close within 5s')

            # Wait for timeline to refresh with new sort
            page.wait_for_timeout(2000)
            # Trigger a tiny scroll to kick X.com's lazy loader
            page.evaluate('window.scrollBy(0, 1)')
            page.wait_for_timeout(1000)
            try:
                page.wait_for_selector('article[data-testid="tweet"]', timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(500)

            # Best-effort verification
            is_chrono = self._verify_timeline_is_chronological(page)
            if is_chrono:
                logger.info('Latest sort confirmed — timeline is chronological')
            else:
                logger.info('Latest sort applied (chronological check inconclusive)')
            return True
        except Exception as exc:
            logger.warning('_ensure_latest_sort failed: %s', exc)
            return False

    def _ensure_following_latest(self, page) -> bool:
        """Switch to Following tab + Latest sort with retry. Returns True on success."""
        for attempt in range(3):
            logger.info('Following+Latest switch attempt %d/3...', attempt + 1)
            if attempt > 0:
                page.goto('https://x.com/home', wait_until='domcontentloaded')
                try:
                    page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                self._dismiss_modals(page)

            if not self._ensure_following_tab(page):
                logger.warning('Following tab switch failed on attempt %d', attempt + 1)
                continue

            if not self._ensure_latest_sort(page):
                logger.warning('Latest sort switch failed on attempt %d', attempt + 1)
                continue

            # Verify timeline actually has content loaded
            article_count = page.locator('article[data-testid="tweet"]').count()
            if article_count == 0:
                logger.warning('Switch succeeded but 0 articles visible on attempt %d — retrying', attempt + 1)
                continue

            logger.info('Following+Latest confirmed on attempt %d (%d articles)', attempt + 1, article_count)
            return True

        logger.error('Failed to switch to Following+Latest after 3 attempts')
        return False

    def _dismiss_modals(self, page):
        try:
            btn = page.locator('[data-testid="app-bar-close"]')
            if btn.count() > 0:
                btn.first.click()
                page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            page.keyboard.press('Escape')
        except Exception:
            pass

    def search(self, query: str, max_tweets: int = 50) -> list:
        """Search X.com public square for a query.

        Navigates to x.com/search?q={query}&src=typed_query&f=top,
        scrolls to load results, extracts tweets via JS evaluation.
        Returns list of ScrapedTweet, or None if not logged in.
        """
        import urllib.parse
        page = self._browser.page
        encoded = urllib.parse.quote(query)
        search_url = f'https://x.com/search?q={encoded}&src=typed_query&f=top'
        logger.info('Searching X: %s', search_url)
        page.goto(search_url, wait_until='domcontentloaded', timeout=60000)

        current = page.url.lower()
        if 'login' in current:
            logger.error('Redirected to login page — session expired')
            return None

        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=12000)
            logger.info('Search results rendered')
        except Exception:
            logger.warning('Search results did not render')
            if page.locator('[data-testid="emptyState"]').count() > 0:
                logger.info('X returned no results for query')
                return []
            return []

        self._human_wait(page, 1000, 2000)
        self._dismiss_modals(page)

        tweets = []
        scrolls = 0
        stale = 0
        max_scrolls = 8

        while scrolls < max_scrolls and len(tweets) < max_tweets and stale < 8:
            articles_result = self._extract_articles_js(page)
            articles_data = articles_result['data']

            for d in articles_data:
                tid = d.get('tid', '')
                if tid in self._seen_ids:
                    continue
                self._seen_ids.add(tid)
                tweet = self._js_dict_to_tweet(d)
                if tweet:
                    tweets.append(tweet)
                    if len(tweets) >= max_tweets:
                        break

            if len(tweets) > 0:
                stale = 0
            else:
                stale += 1

            page.evaluate("""
                (() => {
                    const articles = document.querySelectorAll('article[data-testid="tweet"]');
                    if (articles.length > 0) {
                        articles[articles.length - 1].scrollIntoView(false);
                    }
                })();
            """)
            self._human_wait(page, 400, 900)

            prev_count = page.locator('article[data-testid="tweet"]').count()
            for _ in range(4):
                page.wait_for_timeout(200)
                if page.locator('article[data-testid="tweet"]').count() > prev_count:
                    break

            scrolls += 1
            if scrolls % 3 == 0:
                logger.info('Search scrolled %d, %d tweets', scrolls, len(tweets))

        logger.info('Search done: %d tweets for query "%s"', len(tweets), query)
        return tweets

    def get_following_list(self, my_username, max_pages=10):
        page = self._browser.page
        page.goto(f'https://x.com/{my_username}/following', wait_until='domcontentloaded')
        page.wait_for_timeout(3000)
        accounts = []
        seen = set()
        for _ in range(max_pages):
            for cell in page.locator('[data-testid="cellInnerDiv"]').all():
                try:
                    link = cell.locator('a[href^="/"][role="link"]').first
                    href = link.get_attribute('href') or ''
                    uname = href.strip('/')
                    if uname and uname not in seen and '/' not in uname:
                        seen.add(uname)
                        accounts.append({'username': uname, 'display_name': uname})
                except Exception:
                    continue
            page.evaluate('window.scrollBy(0, 600)')
            page.wait_for_timeout(2000)
        return accounts


