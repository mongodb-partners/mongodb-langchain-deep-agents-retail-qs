import { ChatProvider } from './context/ChatContext';
import HomePage from './pages/HomePage';
import ChatWidget from './components/ChatWidget';

export default function App() {
  return (
    <ChatProvider>
      <HomePage />
      <ChatWidget />
    </ChatProvider>
  );
}
