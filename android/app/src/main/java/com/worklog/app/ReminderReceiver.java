package com.worklog.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.os.Build;
import android.util.Log;
import java.io.File;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 每���提醒的接收端：读本地 SQLite 统计逾期与今日到期，发一条系统通知。
 *
 * 关键点：整个流程不依赖 Python 进程——应用被杀掉后闹钟仍能唤醒本接收器，
 * 这是 Python 后台线程做不到的。
 */
public class ReminderReceiver extends BroadcastReceiver {

    private static final String TAG = "Worklog";
    private static final String CHANNEL_ID = "worklog-daily";

    @Override
    public void onReceive(Context context, Intent intent) {
        boolean force = intent != null && "com.worklog.app.TEST".equals(intent.getAction());
        int overdue = 0;
        int today = 0;
        try {
            int[] counts = countTasks(context);
            overdue = counts[0];
            today = counts[1];
        } catch (Exception e) {
            Log.w(TAG, "读取待办统计失败", e);
        }
        Log.i(TAG, "提醒触发 overdue=" + overdue + " today=" + today + " force=" + force);

        if (overdue == 0 && today == 0 && !force) {
            Log.i(TAG, "没有待办，本次不打扰");
        } else {
            notify(context, overdue, today);
        }
        // 排下一天
        ReminderScheduler.schedule(context);
    }

    private int[] countTasks(Context context) {
        File dbFile = new File(context.getFilesDir(), "app.db");
        if (!dbFile.exists()) {
            return new int[]{0, 0};
        }
        String todayText = new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(new Date());
        SQLiteDatabase db = SQLiteDatabase.openDatabase(
                dbFile.getAbsolutePath(), null, SQLiteDatabase.OPEN_READONLY);
        try {
            int overdue = query(db,
                    "select count(*) from tasks where status!='done' and ifnull(archived,0)=0"
                            + " and due_date is not null and due_date<>'' and due_date<?",
                    new String[]{todayText});
            int dueToday = query(db,
                    "select count(*) from tasks where status!='done' and ifnull(archived,0)=0"
                            + " and due_date=?",
                    new String[]{todayText});
            return new int[]{overdue, dueToday};
        } finally {
            db.close();
        }
    }

    private int query(SQLiteDatabase db, String sql, String[] args) {
        Cursor cursor = db.rawQuery(sql, args);
        try {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        } finally {
            cursor.close();
        }
    }

    private void notify(Context context, int overdue, int today) {
        NotificationManager manager =
                (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "每日提醒", NotificationManager.IMPORTANCE_DEFAULT);
            channel.setDescription("每天提醒逾期与今日到期的工作");
            manager.createNotificationChannel(channel);
        }

        StringBuilder text = new StringBuilder();
        if (overdue > 0) {
            text.append("逾期 ").append(overdue).append(" 项");
        }
        if (today > 0) {
            if (text.length() > 0) {
                text.append(" · ");
            }
            text.append("今天到期 ").append(today).append(" 项");
        }
        if (text.length() == 0) {
            text.append("今天没有到期的工作，可以安心推进计划");
        }

        Intent open = new Intent(context, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent content = PendingIntent.getActivity(context, 2002, open, flags);

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(context, CHANNEL_ID)
                : new Notification.Builder(context);
        builder.setContentTitle("工作志")
                .setContentText(text.toString())
                .setSmallIcon(R.mipmap.ic_launcher)
                .setAutoCancel(true)
                .setContentIntent(content);
        manager.notify(3003, builder.build());
    }
}
