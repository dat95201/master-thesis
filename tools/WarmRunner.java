import java.io.*;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.Charset;
import java.nio.file.*;
import java.util.*;
import javax.tools.*;

/**
 * Module M0, the warm path: one JVM that stays alive for a repair session.
 * For each candidate it compiles the one patched source file with the
 * compiler of this JVM, loads the result through a fresh class loader and
 * runs the scheduled test classes there. The build and the JVM start-up are
 * paid once per session, not once per candidate.
 *
 *   java -cp <dir of this class>:<junit> WarmRunner <cp.test> <cp.compile> <release|-> <encoding> <timeout_s> <scratch dir>
 *
 * Only JUnit and this runner live in the JVM's own class loader. The
 * project, its tests and its libraries are loaded again for every candidate,
 * so no static state of theirs survives from one candidate to the next, and
 * a candidate may change the shape of a class.
 *
 * Commands, one per line on stdin, tab separated:
 *   RUN <id> <source|-> <classes dir|-> <classes.txt> <out.tsv> <all|stop>
 *       source       patched source file to compile into <classes dir>; "-" = do not compile
 *       classes dir  compiled patch, put in front of cp.test; "-" = run the program as built
 *   QUIT
 * Replies on stdout; only lines that start with "@@" are protocol:
 *   @@READY <uptime_ms> <process_cpu_ms>
 *   @@DONE <id> <ok|compile_error|hung> <process_cpu_ms>
 * After "hung" the JVM exits: the stuck test thread cannot be stopped, so
 * the caller starts a new runtime for the remaining classes.
 *
 * out.tsv has the lines of AuditRunner, preceded by
 *   COMPILE <ok|error> <wall_ms> <cpu_ms>
 *   ERR <one line of the first compiler error>      (only after an error)
 */
public class WarmRunner {
    static PrintStream proto;
    static String cpTest, cpCompile, release, encoding;
    static long timeoutMs;
    static Path scratch;

    public static void main(String[] args) throws Exception {
        cpTest = args[0];
        cpCompile = args[1];
        release = args[2].equals("-") ? null : args[2];
        encoding = args[3];
        timeoutMs = (long) (Double.parseDouble(args[4]) * 1000);
        scratch = Paths.get(args[5]);
        Files.createDirectories(scratch.resolve("empty"));

        // The protocol owns the real stdin and stdout; tests get neither.
        proto = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
        PrintStream sink = new PrintStream(new OutputStream() { @Override public void write(int b) { } });
        InputStream noInput = new ByteArrayInputStream(new byte[0]);
        Properties props = (Properties) System.getProperties().clone();
        Locale locale = Locale.getDefault();
        TimeZone zone = TimeZone.getDefault();

        if (ToolProvider.getSystemJavaCompiler() == null) {
            System.err.println("WarmRunner needs a JDK: this runtime has no compiler");
            System.exit(2);
        }
        proto.println("@@READY\t" + java.lang.management.ManagementFactory.getRuntimeMXBean().getUptime()
                + "\t" + cpuMs());

        String line;
        while ((line = in.readLine()) != null) {
            String[] f = line.split("\t");
            if (f[0].equals("QUIT")) break;
            if (!f[0].equals("RUN") || f.length < 7) continue;
            String id = f[1], src = f[2], dir = f[3], status;
            System.setIn(noInput);
            System.setOut(sink);
            System.setErr(sink);
            Set<Thread> before = Thread.getAllStackTraces().keySet();
            try (PrintWriter out = new PrintWriter(new FileWriter(f[5]))) {
                status = run(src, dir, Files.readAllLines(Paths.get(f[4])), out, f[6].equals("stop"));
                out.println("END");
            } catch (Throwable t) {
                status = "error";
                System.err.println("WarmRunner: " + t);
            }
            // JVM-wide state a candidate may have changed
            System.setProperties((Properties) props.clone());
            Locale.setDefault(locale);
            TimeZone.setDefault(zone);
            for (Thread t : Thread.getAllStackTraces().keySet()) {
                if (!before.contains(t) && t.isAlive() && t != Thread.currentThread()) t.interrupt();
            }
            proto.println("@@DONE\t" + id + "\t" + status + "\t" + cpuMs());
            if (status.equals("hung")) System.exit(0);
        }
        System.exit(0);
    }

    static long cpuMs() { return AuditRunner.OS.getProcessCpuTime() / 1_000_000; }

    static String run(String src, String dir, List<String> classes, PrintWriter out, boolean stop)
            throws IOException {
        if (!src.equals("-")) {
            long c0 = AuditRunner.OS.getProcessCpuTime(), t0 = System.nanoTime();
            List<String> errors = compile(src, dir);
            out.println(String.join("\t", "COMPILE", errors.isEmpty() ? "ok" : "error",
                    AuditRunner.fmt((System.nanoTime() - t0) / 1e6),
                    AuditRunner.fmt((AuditRunner.OS.getProcessCpuTime() - c0) / 1e6)));
            for (String e : errors) out.println("ERR\t" + e.replace('\t', ' '));
            if (!errors.isEmpty()) return "compile_error";
        }
        List<URL> urls = new ArrayList<>();
        if (!dir.equals("-")) urls.add(Paths.get(dir).toUri().toURL());
        for (String p : cpTest.split(File.pathSeparator)) {
            if (!p.isEmpty()) urls.add(Paths.get(p).toUri().toURL());
        }
        try (URLClassLoader loader = new URLClassLoader(urls.toArray(new URL[0]),
                WarmRunner.class.getClassLoader())) {
            String s = AuditRunner.runAll(classes, loader, out, stop, timeoutMs, null);
            return s.equals("timeout") ? "hung" : "ok";
        }
    }

    /** Compiles one source file in this JVM. Returns the first error as
     *  lines ("File.java:12: error: ...", then javac's detail lines), or an
     *  empty list. */
    static List<String> compile(String src, String dir) throws IOException {
        Files.createDirectories(Paths.get(dir));
        JavaCompiler jc = ToolProvider.getSystemJavaCompiler();
        DiagnosticCollector<JavaFileObject> diags = new DiagnosticCollector<>();
        List<String> opts = new ArrayList<>(Arrays.asList("-nowarn", "-proc:none", "-g",
                "-encoding", encoding, "-classpath", cpCompile,
                "-sourcepath", scratch.resolve("empty").toString(), "-d", dir));
        if (release != null) opts.addAll(Arrays.asList("-source", release, "-target", release));
        boolean ok;
        try (StandardJavaFileManager fm = jc.getStandardFileManager(diags, Locale.ROOT, Charset.forName(encoding))) {
            ok = jc.getTask(null, fm, diags, opts, null, fm.getJavaFileObjects(new File(src))).call();
        }
        List<String> errors = new ArrayList<>();
        if (ok) return errors;
        for (Diagnostic<? extends JavaFileObject> d : diags.getDiagnostics()) {
            if (d.getKind() != Diagnostic.Kind.ERROR) continue;
            String file = d.getSource() == null ? "" : Paths.get(d.getSource().toUri()).getFileName() + ":"
                    + d.getLineNumber() + ": ";
            String[] msg = d.getMessage(Locale.ROOT).split("\n");
            errors.add(file + "error: " + msg[0]);
            for (int i = 1; i < msg.length && i < 5; i++) errors.add(msg[i]);
            break;
        }
        if (errors.isEmpty()) errors.add("error: compilation failed");
        return errors;
    }
}
