import java.io.*;
import java.lang.management.ManagementFactory;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.runner.*;
import org.junit.runner.notification.*;

/**
 * The cold path: runs JUnit test classes one after another in a JVM started
 * for this one candidate, and records for each class pass/fail, wall time,
 * CPU time and its first failure. WarmRunner reuses runClass() inside a JVM
 * that stays alive.
 *
 *   java -cp <dir of this class>:<junit>:<cp.test> AuditRunner <classes.txt> <out.tsv> <mode> <timeout_s> [probe]
 *
 *   mode     "all"   run every class (an audit)
 *            "stop"  stop after the first failing class (a real scheduled run)
 *   probe    optional system property name; the runner clears it before each
 *            class and reports whether the class set it (method coverage).
 *
 * Output (tab separated, one line per event):
 *   START  <class>
 *   CLASS  <class> <status> <wall_ms> <cpu_ms> <n_run> <n_fail> <test> <exception> <line> <message> <probe_hit>
 *   END
 * status is pass, fail, timeout or error (class could not be loaded).
 * A class that kills the JVM leaves a START line without a CLASS line; the
 * Python side treats it as a failure and restarts with the remaining classes.
 */
public class AuditRunner {
    static final com.sun.management.OperatingSystemMXBean OS =
        (com.sun.management.OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();

    public static void main(String[] args) throws Exception {
        List<String> classes = Files.readAllLines(Paths.get(args[0]));
        boolean stopAtFirst = args[2].equals("stop");
        long timeoutMs = (long) (Double.parseDouble(args[3]) * 1000);
        String probe = args.length > 4 ? args[4] : null;
        try (PrintWriter out = new PrintWriter(new FileWriter(args[1]))) {
            runAll(classes, AuditRunner.class.getClassLoader(), out, stopAtFirst, timeoutMs, probe);
            out.println("END");
        }
        // Some test suites leave non-daemon threads behind; Ant also calls exit.
        System.exit(0);
    }

    /** Runs the classes in order. Returns "timeout" if a class hung (its
     *  thread cannot be stopped, so the JVM must not be reused), else "ok". */
    static String runAll(List<String> classes, ClassLoader loader, PrintWriter out,
                         boolean stopAtFirst, long timeoutMs, String probe) {
        for (String cls : classes) {
            cls = cls.trim();
            if (cls.isEmpty()) continue;
            out.println("START\t" + cls);
            out.flush();
            String status = runClass(cls, loader, out, timeoutMs, probe);
            out.flush();
            if (status.equals("timeout")) return "timeout";
            if (stopAtFirst && !status.equals("pass")) break;
        }
        return "ok";
    }

    static String clean(String s, int max) {
        if (s == null) return "";
        s = s.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ');
        return s.length() > max ? s.substring(0, max) + "..." : s;
    }

    static String runClass(String cls, ClassLoader loader, PrintWriter out, long timeoutMs, String probe) {
        if (probe != null) System.clearProperty(probe);
        Class<?> c;
        try {
            c = Class.forName(cls, false, loader);
        } catch (Throwable t) {
            out.println(String.join("\t", "CLASS", cls, "error", "0", "0", "0", "0",
                    "-", t.getClass().getName(), "0", clean(t.toString(), 300), "0"));
            return "error";
        }
        AtomicReference<Result> result = new AtomicReference<>();
        AtomicReference<Failure> first = new AtomicReference<>();
        long cpu0 = OS.getProcessCpuTime();
        long t0 = System.nanoTime();
        Thread worker = new Thread(() -> {
            JUnitCore core = new JUnitCore();
            core.addListener(new RunListener() {
                @Override public void testFailure(Failure f) { first.compareAndSet(null, f); }
            });
            result.set(core.run(Request.aClass(c)));
        });
        worker.setContextClassLoader(loader);
        worker.setDaemon(false);   // threads made by tests inherit this flag
        worker.start();
        try { worker.join(timeoutMs); } catch (InterruptedException e) { /* ignore */ }
        double wallMs = (System.nanoTime() - t0) / 1e6;
        double cpuMs = (OS.getProcessCpuTime() - cpu0) / 1e6;
        String hit = probe != null && System.getProperty(probe) != null ? "1" : "0";

        if (worker.isAlive()) {
            out.println(String.join("\t", "CLASS", cls, "timeout", fmt(wallMs), fmt(cpuMs), "0", "1",
                    "-", "Timeout", "0", "test class exceeded " + timeoutMs / 1000 + " s", hit));
            return "timeout";
        }
        Result r = result.get();
        Failure f = first.get();
        String status = (r != null && r.getFailureCount() == 0 && f == null) ? "pass" : "fail";
        String test = "-", exc = "-", line = "0", msg = "";
        if (f != null) {
            Description d = f.getDescription();
            test = d.getMethodName() != null ? d.getMethodName() : "-";
            Throwable e = f.getException();
            if (e != null) {
                exc = e.getClass().getName();
                msg = clean(e.getMessage(), 400);
                String testClass = d.getClassName() != null ? d.getClassName() : cls;
                for (StackTraceElement el : e.getStackTrace()) {
                    if (el.getClassName().equals(testClass) || el.getClassName().equals(cls)) {
                        line = Integer.toString(el.getLineNumber());
                        break;
                    }
                }
            }
        }
        int nRun = r == null ? 0 : r.getRunCount();
        int nFail = r == null ? 1 : r.getFailureCount();
        out.println(String.join("\t", "CLASS", cls, status, fmt(wallMs), fmt(cpuMs),
                Integer.toString(nRun), Integer.toString(nFail), clean(test, 200), exc, line, msg, hit));
        return status;
    }

    static String fmt(double v) { return String.format(Locale.ROOT, "%.3f", v); }
}
