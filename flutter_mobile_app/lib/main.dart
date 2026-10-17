import 'package:flutter/material.dart';

import 'config/app_config.dart';
import 'screens/app_shell.dart';
import 'screens/login_screen.dart';
import 'services/auth_service.dart';
import 'services/jobs_service.dart';
import 'services/management_service.dart';
import 'services/tasks_service.dart';
import 'services/token_store.dart';
import 'theme/app_theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await AppConfig.initialize();
  runApp(const MobileApp());
}

class MobileApp extends StatefulWidget {
  const MobileApp({super.key});

  @override
  State<MobileApp> createState() => _MobileAppState();
}

class _MobileAppState extends State<MobileApp> {
  final AuthService _authService = AuthService(TokenStore());
  late final JobsService _jobsService = JobsService(_authService);
  late final ManagementService _managementService = ManagementService(
    _authService,
  );
  late final TasksService _tasksService = TasksService(_authService);

  bool _isLoading = true;
  bool _isAuthenticated = false;

  @override
  void initState() {
    super.initState();
    _bootstrapSession();
  }

  Future<void> _bootstrapSession() async {
    final hasSession = await _authService.restoreSession();
    if (!mounted) {
      return;
    }
    setState(() {
      _isLoading = false;
      _isAuthenticated = hasSession;
    });
  }

  void _onLoggedIn() {
    setState(() {
      _isAuthenticated = true;
    });
  }

  Future<void> _onLogout() async {
    await _authService.logout();
    if (!mounted) {
      return;
    }
    setState(() {
      _isAuthenticated = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      title: 'ZynDesk',
      theme: AppTheme.light(),
      home: _isLoading
          ? const Scaffold(body: Center(child: CircularProgressIndicator()))
          : (_isAuthenticated
                ? AppShell(
                    authService: _authService,
                    jobsService: _jobsService,
                    managementService: _managementService,
                    tasksService: _tasksService,
                    onLogout: _onLogout,
                  )
                : LoginScreen(
                    authService: _authService,
                    onLoginSuccess: _onLoggedIn,
                  )),
    );
  }
}
