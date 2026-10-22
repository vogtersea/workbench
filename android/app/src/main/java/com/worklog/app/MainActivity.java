package com.worklog.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 桌面壳的 Android 版：起本地服务 -> 等就绪 -> WebView 显示。
 *
 * 三个缺一不可的点：
 *   1. setWebViewClient —— 不设它，WebView 把每次导航交给系统浏览器，
 *      表现为「点按钮就跳到浏览器」，应用内根本没法用。
 *   2. setWebChromeClient —— JS 的 alert/confirm 没有它会被静默忽略，
 *      而删除确认用的正是 confirm()。
 *   3. onBackPressed —— 让返回键在 WebView 内部后退，而不是直接退出应用。
 */
public class MainActivity extends Activity {

    private static final String TAG = "Worklog";
    private WebView webView;

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
        setContentView(webView);

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
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton("确定", (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton("确定", (d, w) -> result.confirm())
                        .setNegativeButton("取消", (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel())
                        .show();
                return true;
            }
        });

        new Thread(this::startServerAndLoad).start();
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

            for (int i = 0; i < 100; i++) {
                if (isHealthy(baseUrl + "/healthz")) break;
                Thread.sleep(200);
            }
            final String url = baseUrl + "/";
            runOnUiThread(() -> webView.loadUrl(url));
        } catch (Throwable t) {
            Log.e(TAG, "启动失败", t);
            final String message = "启动失败：" + t;
            runOnUiThread(() -> webView.loadDataWithBaseURL(null,
                    "<html><body style='font-family:sans-serif;padding:24px'>"
                            + "<h3>启动失败</h3><pre>" + message + "</pre></body></html>",
                    "text/html", "utf-8", null));
        }
    }

    @Override
    public void onBackPressed() {
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
}
