package com.worklog.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Android 壳：开屏界面 -> 后台起本地服务 -> 就绪后切 WebView。
 *
 * 三个必须设置的项（缺一即坏）：
 *   1. setWebViewClient  不设则每次导航交给系统浏览器，表现为「点按钮就跳浏览器」
 *   2. setWebChromeClient 不设则 JS confirm() 静默返回 false，删除确认失效
 *   3. onBackPressed     不处理则返回键直接退出应用
 */
public class MainActivity extends Activity {
    private static final int MATCH_PARENT = ViewGroup.LayoutParams.MATCH_PARENT;

    private static final String TAG = "Worklog";
    private static final int BRAND = 0xFF3B5BDB;
    private WebView webView;
    private LinearLayout splash;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setSupportMultipleWindows(false);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String host = uri.getHost();
                if (host != null && (host.equals("127.0.0.1") || host.equals("localhost"))) {
                    return false;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                } catch (Exception e) {
                    Log.w(TAG, "无法打开外部链接: " + uri, e);
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                // 关键：等页面真正渲染完再撤掉开屏，否则会看到一段空白 WebView
                runOnUiThread(() -> setContentView(webView));
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message)
                        .setPositiveButton("确定", (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel()).show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message)
                        .setPositiveButton("确定", (d, w) -> result.confirm())
                        .setNegativeButton("取消", (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel()).show();
                return true;
            }
        });

        requestNotificationPermission();
        ReminderScheduler.schedule(this);
        setContentView(buildSplash(), new ViewGroup.LayoutParams(MATCH_PARENT, MATCH_PARENT));
        new Thread(this::startServerAndLoad).start();
    }

    /** 开屏界面：品牌色底 + 图标 + 名称 + 启动提示。 */
    private LinearLayout buildSplash() {
        splash = new LinearLayout(this);
        splash.setOrientation(LinearLayout.VERTICAL);
        splash.setGravity(Gravity.CENTER);
        splash.setBackgroundColor(BRAND);
        splash.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.mipmap.ic_launcher);
        int size = dp(96);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(size, size);
        lp.bottomMargin = dp(18);
        logo.setLayoutParams(lp);
        splash.addView(logo);

        TextView name = new TextView(this);
        name.setText("工作志");
        name.setTextColor(Color.WHITE);
        name.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        name.setTypeface(Typeface.DEFAULT_BOLD);
        name.setGravity(Gravity.CENTER);
        splash.addView(name);

        TextView tagline = new TextView(this);
        tagline.setText("个人工作流程跟踪 · 测试版");
        tagline.setTextColor(0xCCFFFFFF);
        tagline.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        tagline.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp2.topMargin = dp(6);
        tagline.setLayoutParams(lp2);
        splash.addView(tagline);

        TextView hint = new TextView(this);
        hint.setText("正在启动本地服务…");
        hint.setTextColor(0x99FFFFFF);
        hint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        hint.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams lp3 = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp3.topMargin = dp(28);
        hint.setLayoutParams(lp3);
        splash.addView(hint);
        return splash;
    }

    private int dp(int value) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                getResources().getDisplayMetrics());
    }

    private void startServerAndLoad() {
        try {
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(this));
            }
            String dataDir = getFilesDir().getAbsolutePath();
            PyObject module = Python.getInstance().getModule("launcher");
            int port = module.callAttr("start", dataDir).toInt();
            String baseUrl = "http://127.0.0.1:" + port;

            for (int i = 0; i < 150; i++) {
                if (isHealthy(baseUrl + "/healthz")) break;
                Thread.sleep(200);
            }
            final String url = baseUrl + "/";
            runOnUiThread(() -> webView.loadUrl(url));  // 切换交给 onPageFinished
        } catch (Throwable t) {
            Log.e(TAG, "启动失败", t);
            final String message = String.valueOf(t);
            runOnUiThread(() -> webView.loadDataWithBaseURL(null,
                    "<html><body style='font-family:sans-serif;padding:24px'>"
                            + "<h3>启动失败</h3><pre>" + message + "</pre></body></html>",
                    "text/html", "utf-8", null));
        }
    }

    @Override
    public void onBackPressed() {
        if (getWindow().getDecorView().findViewById(android.R.id.content).equals(webView.getParent())
                && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    private boolean isHealthy(String url) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(800);
            conn.setReadTimeout(800);
            int code = conn.getResponseCode();
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()))) {
                while (reader.readLine() != null) { }
            }
            return code == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /** Android 13+ 需要运行时申请通知权限，否则提醒发不出来。 */
    private void requestNotificationPermission() {
        if (android.os.Build.VERSION.SDK_INT >= 33
                && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 100);
        }
    }
}
